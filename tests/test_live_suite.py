"""The live suite's own logic, tested offline (issue #39): every scenario in plan mode with
nothing started, the runner's gates, preflight and run mode against the pinned fake forgectl, the
signal and pty helpers, and the checklist against the scenarios and docs/live-checklist.md. No
host is reached."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pr_comment.cli import build_parser, validate_args
from tests import forgectl_harness as h
from tests.forgectl_harness import FakeForgectl, fail, ok
from tests.live import checklist, helpers
from tests.live import runner as runner_module
from tests.live.ctx import Ctx
from tests.live.driver import cases, run_scenario
from tests.live.runner import LiveFailure, LiveRefusal, Outcome, Runner, Step
from tests.live.scenarios import SCENARIOS, Scenario, skip_reason
from tests.live.settings import Target, from_env

ROOT = Path(__file__).resolve().parents[1]
REPO = "github.com/acme/widgets"


def settings_for(mode: str, tmp_path: Path, **extra: str) -> Any:
    return from_env(
        {
            "PR_COMMENT_LIVE": "1",
            "PR_COMMENT_LIVE_MODE": mode,
            "PR_COMMENT_LIVE_ALLOW": REPO,
            "PR_COMMENT_LIVE_GITHUB_PR": h.PR,
            "PR_COMMENT_LIVE_REPORT_DIR": str(tmp_path / "report"),
            "PR_COMMENT_LIVE_RUN_ID": "testrun1",
            **extra,
        }
    )


class NothingStarts:
    """Every way the runner starts a process fails the test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError(f"a process was started in plan mode: {args[:1]}")

        monkeypatch.setattr(runner_module.subprocess, "run", boom)
        monkeypatch.setattr(runner_module.subprocess, "Popen", boom)
        monkeypatch.setattr(runner_module, "run_on_pty", boom)


# --- plan mode: every scenario, nothing started ---------------------------------------------------

PLAN = from_env({"PR_COMMENT_LIVE": "1", "PR_COMMENT_LIVE_RUN_ID": "planrun1"})
PLAN_CASES = cases(PLAN)


def _id(case: tuple[Scenario, Target]) -> str:
    return f"{case[0].id}-{case[1].name}"


@pytest.mark.parametrize("case", PLAN_CASES, ids=[_id(c) for c in PLAN_CASES])
def test_a_scenario_in_plan_mode_records_its_steps_and_starts_nothing(
    case: tuple[Scenario, Target], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    NothingStarts(monkeypatch)
    scenario, target = case
    if target.backend not in scenario.backends:
        pytest.skip("not for this host")
    secret = "tok_abcdef0123456789"
    runner = run_scenario(
        PLAN, scenario, target, tmp_path / "work", env={"PATH": "/nonexistent", "GH_TOKEN": secret}
    )
    assert runner.steps, "a scenario with no step settles nothing"
    assert {s.applied for s in runner.steps} == {"plan"}
    text = "\n".join(runner.describe())
    assert secret not in text
    if scenario.writes:
        assert any(s.write for s in runner.steps)


@pytest.mark.parametrize("case", PLAN_CASES, ids=[_id(c) for c in PLAN_CASES])
def test_every_planned_pr_comment_command_parses_with_pr_comments_own_parser(
    case: tuple[Scenario, Target], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in a planned argv (an unknown flag, a value the grammar refuses) fails here, not
    on the maintainer's first run."""
    NothingStarts(monkeypatch)
    scenario, target = case
    if target.backend not in scenario.backends:
        pytest.skip("not for this host")
    runner = run_scenario(PLAN, scenario, target, tmp_path / "work", env={"PATH": "/nonexistent"})
    checked = 0
    for step in runner.steps:
        if step.tool != "pr-comment" or any(re.fullmatch(r"<[a-z_]+>", a) for a in step.argv):
            continue
        args = build_parser().parse_args([*step.argv, "--json", "--config", "/dev/null"])
        validate_args(args)
        checked += 1
    assert checked or all(s.tool != "pr-comment" for s in runner.steps)


def test_the_scenarios_are_registered_with_rows_hosts_and_a_description() -> None:
    assert len(SCENARIOS) >= 20
    for scenario in SCENARIOS.values():
        assert scenario.rows, scenario.id
        assert scenario.backends and set(scenario.backends) <= {"github", "gitlab"}
        assert scenario.doc and len(scenario.doc) < 100, scenario.id


def test_scenarios_that_need_something_are_skipped_without_it(tmp_path: Path) -> None:
    settings = settings_for("run", tmp_path)
    target = settings.targets["GITHUB"]
    reasons = {sc.id: skip_reason(sc, settings, target) for sc in SCENARIOS.values()}
    assert "OLD_HEAD" in str(reasons["stale_and_reanchor"])
    assert "CLOSED_PR" in str(reasons["closed_pr"])
    assert "APPROVE" in str(reasons["approve_interactive"])
    assert "SLOW" in str(reasons["deadline_kill"])
    assert "gitlab" in str(reasons["gitlab_body_thread"])
    assert reasons["comment_post"] is None


# --- the runner's gates ---------------------------------------------------------------------------


def test_a_write_to_an_unverified_target_never_runs(forgectl: FakeForgectl, tmp_path: Path) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())
    with pytest.raises(LiveRefusal, match="verified"):
        runner.pc("c", "comment", h.PR, "--body-file", "-", stdin=b"x")
    with pytest.raises(LiveRefusal, match="verified"):
        runner.fg("c", "comment", "--json", h.PR, "--body-file", "-", stdin=b"x", write=True)
    assert forgectl.calls() == []


def test_a_target_off_the_allowlist_is_refused_before_any_call(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())
    with pytest.raises(LiveRefusal, match="ALLOW"):
        runner.verify_target("github.com/acme/other#1")
    assert forgectl.calls() == []


def test_a_target_whose_canonical_repo_is_off_the_allowlist_is_refused_after_show(
    forgectl: FakeForgectl, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())

    def renamed(label: str, *argv: str, **kw: Any) -> Outcome:
        step = Step("s", label, "forgectl", list(argv), False)
        return Outcome(step, 0, {"repo": "github.com/acme/elsewhere"}, real=True)

    monkeypatch.setattr(runner, "fg", renamed)  # the fake refuses to answer for another repo
    with pytest.raises(LiveRefusal, match="canonical"):
        runner.verify_target(h.PR)
    assert forgectl.writes() == [] and h.PR not in runner.verified


def test_a_target_forgectl_cannot_show_is_refused(forgectl: FakeForgectl, tmp_path: Path) -> None:
    forgectl.script(fail("show", "not_found"))
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())
    with pytest.raises(LiveRefusal, match="could not confirm"):
        runner.verify_target(h.PR)
    assert h.PR not in runner.verified


def test_a_probe_ref_is_checked_by_its_text_only(forgectl: FakeForgectl, tmp_path: Path) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())
    runner.allow_ref_text(f"{REPO}#99")
    with pytest.raises(LiveRefusal):
        runner.allow_ref_text("github.com/acme/other#99")
    assert forgectl.calls() == []


def test_a_step_with_a_placeholder_is_deferred_in_preflight(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    runner = Runner(settings_for("preflight", tmp_path), tmp_path / "work", env=forgectl.environ())
    out = runner.pc("r", "resolve", h.PR, "--thread", "<thread_id>")
    assert not out.real and runner.steps[0].applied == "deferred"
    assert forgectl.calls() == []


def test_a_step_shows_the_final_arguments_and_redacts_secrets(tmp_path: Path) -> None:
    settings = settings_for("plan", tmp_path)
    runner = Runner(
        settings, tmp_path / "work", env={"PATH": "/x", "SLACK_WEBHOOK_URL": "hook-secret-1"}
    )
    runner.pc("c", "comment", "hook-secret-1", "--body-file", "-", stdin=b"abc")
    (line,) = runner.describe()
    assert "hook-secret-1" not in line and "<SLACK_WEBHOOK_URL>" in line
    assert "--yes" in line and "--actor auto:live-suite" in line
    assert "stdin 3 bytes" in line and "abc" not in line and "(WRITE)" in line


def test_a_resolve_failure_is_read_from_the_threads_document() -> None:
    step = Step("s", "l", "pr-comment", [], True)
    doc = {"threads": [{"thread_id": "x", "error": {"code": "not_found"}}]}
    out = Outcome(step, 10, doc, real=True)
    assert out.error_code == "not_found"
    assert Outcome(step, 0, {"threads": [{"error": None}]}, real=True).error == {}


# --- preflight and run mode against the pinned fake forgectl --------------------------------------


def _probe(ctx: Ctx) -> None:
    out = ctx.comment("p", ctx.text("p", "hello"))
    ctx.want(out, 0)
    ctx.observe("x.row", "ids", ctx.ids(out))
    ctx.observe("x.row", "dedupe_key", ctx.value(out, "dedupe_key"))


PROBE = Scenario("probe", _probe, ("x.row",), ("github",), True, (), "A probe.")


def _show() -> dict[str, Any]:
    return ok("show", h.show_doc(h.PR))


def test_preflight_runs_the_reads_and_turns_every_write_into_a_dry_run(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("preflight", tmp_path)
    forgectl.script(_show(), _show(), ok("comment", h.post_doc("comment", dry_run=True)))
    runner = run_scenario(
        settings, PROBE, settings.targets["GITHUB"], tmp_path / "work", env=forgectl.environ()
    )
    assert forgectl.writes() == []
    assert [s.applied for s in runner.steps] == ["read", "dry-run"]
    assert "--dry-run" in runner.steps[-1].argv and "--yes" not in runner.steps[-1].argv
    report = json.loads(
        next((tmp_path / "report").glob("results-testrun1-probe-github.json")).read_text()
    )
    assert report["mode"] == "preflight" and report["status"] == "passed"
    assert runner.ledger == []


def test_run_mode_posts_through_the_tap_records_the_ledger_and_calls_the_cleanup(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    cleanup = tmp_path / "cleanup.sh"
    seen = tmp_path / "cleanup-args.txt"
    cleanup.write_text(f'#!/bin/sh\necho "$1 $2" >> {seen}\n', encoding="utf-8")
    cleanup.chmod(0o700)
    settings = settings_for("run", tmp_path, PR_COMMENT_LIVE_CLEANUP_CMD=str(cleanup))
    forgectl.script(
        _show(),
        _show(),
        ok("comment", h.post_doc("comment", dry_run=True)),
        ok("comment", h.post_doc("comment", ids=["gh:ic:IC_1"])),
    )
    runner = run_scenario(
        settings, PROBE, settings.targets["GITHUB"], tmp_path / "work", env=forgectl.environ()
    )
    (write,) = forgectl.writes()
    assert "--yes" in write.argv and "--actor=auto:live-suite" in write.argv
    assert [r["id"] for r in runner.ledger] == ["gh:ic:IC_1"]
    ledger = tmp_path / "report" / "ledger-testrun1.jsonl"
    assert json.loads(ledger.read_text().splitlines()[0])["pr"] == h.PR
    assert seen.read_text().split() == [h.PR, "gh:ic:IC_1"]
    assert runner.notes == []
    # what pr-comment passed to forgectl is what the tap logged, in the documented forms
    taps = runner.tap_calls()
    assert [argv[0] for argv in taps] == ["show", "comment", "--version", "comment"]
    for argv in taps:
        assert helpers.flag_form_problems(argv) == [], argv
    assert any(a.startswith("--dedupe-key=prc:comment:") for argv in taps for a in argv)
    report = json.loads(next((tmp_path / "report").glob("results-*-probe-github.json")).read_text())
    assert report["observations"][0] == {"row": "x.row", "key": "ids", "value": ["gh:ic:IC_1"]}


def test_without_a_cleanup_command_the_posted_ids_are_left_for_a_human(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("run", tmp_path)
    forgectl.script(
        _show(),
        _show(),
        ok("comment", h.post_doc("comment", dry_run=True)),
        ok("comment", h.post_doc("comment", ids=["gh:ic:IC_1"])),
    )
    runner = run_scenario(
        settings, PROBE, settings.targets["GITHUB"], tmp_path / "work", env=forgectl.environ()
    )
    assert any("gh:ic:IC_1" in n and "CLEANUP_CMD" in n for n in runner.notes)


def test_a_failed_check_in_run_mode_fails_the_scenario_and_still_writes_the_report(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("run", tmp_path)

    def failing(ctx: Ctx) -> None:
        ctx.check(False, "the host did something odd", "x.row")

    scenario = Scenario("failing", failing, ("x.row",), ("github",), True, (), "Fails.")
    forgectl.script(_show())
    with pytest.raises(LiveFailure, match="odd.*x.row"):
        run_scenario(
            settings,
            scenario,
            settings.targets["GITHUB"],
            tmp_path / "work",
            env=forgectl.environ(),
        )
    report = json.loads(
        next((tmp_path / "report").glob("results-*-failing-github.json")).read_text()
    )
    assert report["status"] == "failed: LiveFailure"


def test_stdout_that_is_not_one_json_document_fails_the_step(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("run", tmp_path)
    runner = Runner(settings, tmp_path / "work", env=forgectl.environ())
    with pytest.raises(LiveFailure, match="not one JSON document"):
        runner.fg("version", "--version", config="")  # prints text, not JSON
    text = runner.fg("version-text", "--version", config="", strict=False)
    assert text.stdout.startswith("forgectl ")


def test_a_signal_during_a_write_is_forwarded_to_the_exact_child_and_the_run_is_outcome_unknown(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("run", tmp_path)
    forgectl.script(
        _show(),
        _show(),
        ok("comment", h.post_doc("comment", dry_run=True)),
        ok(
            "comment",
            h.post_doc("comment", ids=["gh:ic:IC_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    runner = Runner(settings, tmp_path / "work", env=forgectl.environ())
    try:
        runner.verify_target(h.PR)
        n = forgectl.next_call_number() + 3  # show, the pre-check, --version, the write
        running = runner.start_pc("c", "comment", h.PR, "--body-file", "-", stdin=b"signal me")
        assert running.pid is not None
        forgectl.wait_ready(n)
        assert runner.wait_for_audit("attempted", timeout=30)
        assert running.send(signal.SIGTERM)
        out = running.finish(timeout=60)
    finally:
        runner.teardown()
    assert out.code == 12 and out.error_code == "outcome_unknown"
    assert out.error.get("retry") == "same_command"
    assert [r["result"] for r in runner.audit_records()] == ["attempted", "outcome_unknown"]
    assert running.send(signal.SIGTERM) is False  # it is gone: nothing is signaled twice


def test_an_interactive_approval_is_declined_then_confirmed_on_a_pty(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("run", tmp_path)
    dry = ok("comment", h.post_doc("comment", dry_run=True))
    forgectl.script(
        _show(),
        _show(),
        dry,
        _show(),
        dry,
        ok("comment", h.post_doc("comment", ids=["gh:ic:IC_2"])),
    )
    body = tmp_path / "body.txt"
    body.write_text("approved text", encoding="utf-8")
    runner = Runner(settings, tmp_path / "work", env=forgectl.environ())
    try:
        runner.verify_target(h.PR)
        args = ("comment", h.PR, "--body-file", str(body))
        declined = runner.pc_pty("declined", *args, answer=b"n\n")
        assert (declined.code, declined.error_code) == (13, "confirmation_required")
        assert forgectl.writes() == []
        confirmed = runner.pc_pty("confirmed", *args, answer=b"y\n")
    finally:
        runner.teardown()
    assert confirmed.code == 0 and confirmed.doc is not None
    assert confirmed.doc["ids"] == ["gh:ic:IC_2"]
    assert "Post? [y/N]" in confirmed.stderr
    (write,) = forgectl.writes()
    assert "--yes" in write.argv


def test_the_renderer_reads_nothing_unless_enabled_and_never_in_plan_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    NothingStarts(monkeypatch)
    ctx = Ctx(Runner(PLAN, tmp_path / "w", env={"PATH": "/x"}), PLAN_CASES[0][1], "t")
    assert ctx.render("gh:ic:IC_1") is None and ctx.render("<comment_id>") is None


# --- the checklist --------------------------------------------------------------------------------


def test_every_row_has_an_automated_check_or_a_manual_step() -> None:
    for row in checklist.ROWS:
        assert checklist.automated(row.id) or row.manual, row.id


def test_every_row_id_is_unique_and_well_formed() -> None:
    ids = [row.id for row in checklist.ROWS]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[a-z]{2}\.[a-z0-9_]+", i) for i in ids)


def test_scenarios_name_only_rows_that_exist() -> None:
    for scenario in SCENARIOS.values():
        assert set(scenario.rows) <= checklist.ROW_IDS, (
            scenario.id,
            set(scenario.rows) - checklist.ROW_IDS,
        )


def test_every_row_a_scenario_checks_or_observes_is_one_it_declares() -> None:
    """A row id written into a check or an observation must be in the scenario's ``rows``, so
    the table in docs/live-checklist.md says which scenario settles it."""
    source = (ROOT / "tests" / "live" / "scenarios.py").read_text(encoding="utf-8")
    sections = re.split(r"(?m)^@scenario", source)[1:]
    for section in sections:
        name = re.search(r"def s_(\w+)", section)
        assert name
        scenario = SCENARIOS[name.group(1)]
        used = set(re.findall(r'"([a-z]{2}\.[a-z0-9_]+)"', section))
        assert used <= set(scenario.rows), (scenario.id, sorted(used - set(scenario.rows)))
        assert used <= checklist.ROW_IDS, (scenario.id, sorted(used - checklist.ROW_IDS))


def test_the_tests_a_row_names_exist() -> None:
    for row in checklist.ROWS:
        for ref in row.tests:
            path, _, name = ref.partition("::")
            text = (ROOT / path).read_text(encoding="utf-8")
            assert re.search(rf"(?m)^def {re.escape(name)}\b", text), ref


def test_the_checklist_document_is_generated_and_current() -> None:
    assert checklist.DOC.read_text(encoding="utf-8") == checklist.render(), (
        "run: uv run python -m tests.live.checklist --write"
    )


def test_the_document_names_every_variable_the_settings_read() -> None:
    source = (ROOT / "tests" / "live" / "settings.py").read_text(encoding="utf-8")
    read = set(re.findall(r'"(PR_COMMENT_LIVE[A-Z_]*)"', source))
    read |= {f"PR_COMMENT_LIVE_{key}_PR" for key in ("GITHUB", "GITLAB")}
    documented = {name for name, _ in checklist.ENV_VARS}
    missing = {v for v in read if v not in documented and "{" not in v}
    assert not missing, missing


def test_the_document_and_the_suite_name_no_real_owner_host_or_person() -> None:
    texts = [checklist.render()] + [
        p.read_text(encoding="utf-8")
        for p in (ROOT / "tests" / "live").glob("*.py")
        if p.name != "settings.py"
    ]
    allowed_owners = {
        "acme",
        "octocat",
        "example",
        "owner",
        "group",
        "somebody-else",
        "user-attachments",
    }
    for text in texts:
        for match in re.finditer(r"(?:github|gitlab)\.com/([A-Za-z0-9_.<>-]+)", text):
            assert match.group(1).strip("<>").lower() in allowed_owners, match.group(0)
        for match in re.finditer(r"https?://([A-Za-z0-9.-]+)", text):
            assert match.group(1).endswith(("example.com", "github.com", "gitlab.com")), (
                match.group(0)
            )


def test_no_file_of_the_suite_holds_an_invisible_or_separator_character_literally() -> None:
    bad = re.compile("[  \u0085 ​-‏‪-‮⁠﻿\x0b\x0c]")
    for path in [*(ROOT / "tests" / "live").glob("*.py"), ROOT / "tests" / "test_live_settings.py"]:
        assert not bad.search(path.read_text(encoding="utf-8")), path


# --- the pytest entry point -----------------------------------------------------------------------


def _pytest_live(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    home = Path(env["HOME"])
    full = {
        "PATH": os.path.dirname(sys.executable),
        "XDG_CONFIG_HOME": str(home / ".config"),
        **env,
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "live",
            "tests/live/test_live_hosts.py",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            *args,
        ],
        capture_output=True,
        text=True,
        env=full,
        cwd=ROOT,
        timeout=300,
        check=False,
    )


def test_without_the_opt_in_every_host_test_is_skipped_with_its_reason(tmp_path: Path) -> None:
    done = _pytest_live(env={"HOME": str(tmp_path)})
    assert done.returncode == 0, done.stdout[-2000:]
    assert "PR_COMMENT_LIVE=1" in done.stdout and "passed" not in done.stdout.split("\n")[-2]


def test_opted_in_without_an_allowlist_the_suite_refuses_by_variable_name(tmp_path: Path) -> None:
    done = _pytest_live(
        env={"HOME": str(tmp_path), "PR_COMMENT_LIVE": "1", "PR_COMMENT_LIVE_MODE": "run"}
    )
    assert done.returncode == 1, done.stdout[-2000:]
    assert "PR_COMMENT_LIVE_ALLOW" in done.stdout


def test_opted_in_in_plan_mode_prints_the_steps_and_passes(tmp_path: Path) -> None:
    done = _pytest_live("-s", env={"HOME": str(tmp_path), "PR_COMMENT_LIVE": "1"})
    assert done.returncode == 0, done.stdout[-3000:]
    assert "(WRITE)" in done.stdout and "[plan]" in done.stdout
    assert "pr-comment comment" in done.stdout
