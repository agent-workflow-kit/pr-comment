"""The live suite's own logic, tested offline (issue #39): every scenario in plan mode with
nothing started, the runner's gates, preflight and run mode against the pinned fake forgectl, the
signal and pty helpers, and the checklist against the scenarios and docs/live-checklist.md. No
host is reached."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pr_comment.cli import build_parser, validate_args
from tests import forgectl_harness as h
from tests.forgectl_harness import FakeForgectl, fail, ok
from tests.live import checklist, helpers
from tests.live import runner as runner_module
from tests.live.ctx import Ctx
from tests.live.driver import cases, run_scenario, targets_for
from tests.live.pty_run import _prompt_is_up, run_on_pty
from tests.live.report import scrub
from tests.live.runner import (
    LiveFailure,
    LiveRefusal,
    Outcome,
    Runner,
    Step,
    landed_ids,
)
from tests.live.scenarios import SCENARIOS, Scenario, async_hook_config, skip_reason
from tests.live.settings import Target, from_env, redact

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


PLACEHOLDER = re.compile(r"<[a-z_]+>")
DUMMIES = {
    "github": {"<thread_id>": "gh:rt:PRRT_kwDOdummy", "<comment_id>": "gh:ic:IC_kwDOdummy"},
    "gitlab": {"<thread_id>": "gl:d:" + "0" * 40, "<comment_id>": "gl:n:1"},
}


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
    steps = [s for s in runner.steps if s.tool == "pr-comment"]
    for step in steps:
        argv = [
            DUMMIES[target.backend].get(a, "dummy") if PLACEHOLDER.fullmatch(a) else a
            for a in step.argv
        ]
        args = build_parser().parse_args([*argv, "--json", "--config", "/dev/null"])
        validate_args(args)
        checked += 1
    assert checked == len(steps)  # a placeholder step is checked too, with a schema-valid dummy


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
            f"--basetemp={home / 'basetemp'}",
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
    assert "PR_COMMENT_LIVE=1" in done.stdout
    assert not re.search(r"\b\d+ passed\b", done.stdout) and re.search(
        r"\b\d+ skipped\b", done.stdout
    )


def test_opted_in_without_an_allowlist_the_suite_refuses_by_variable_name(tmp_path: Path) -> None:
    done = _pytest_live(
        env={"HOME": str(tmp_path), "PR_COMMENT_LIVE": "1", "PR_COMMENT_LIVE_MODE": "run"}
    )
    assert done.returncode == 1, done.stdout[-2000:]
    assert "PR_COMMENT_LIVE_ALLOW" in done.stdout


def test_opted_in_in_plan_mode_prints_the_steps_and_passes(tmp_path: Path) -> None:
    done = _pytest_live("-s", env={"HOME": str(tmp_path), "PR_COMMENT_LIVE": "1"})
    assert done.returncode == 0, done.stdout[-3000:]
    assert re.search(r"\b\d+ passed\b", done.stdout)
    assert "(WRITE)" in done.stdout and "[plan]" in done.stdout
    assert "pr-comment comment" in done.stdout


# --- review round 1: the runner, the ledger and the redaction --------------------------------

SENTINEL = "sentinel-secret-0123456789"
SCRIPTED = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ["show"]:
    json.dump({{"repo": "github.com/acme/widgets", "state": "open"}}, sys.stdout)
    sys.exit(0)
with open(os.path.join(here, "answer.json"), encoding="utf-8") as handle:
    answer = json.load(handle)
if answer.get("sleep"):
    time.sleep(answer["sleep"])
sys.stderr.write(answer.get("stderr", ""))
sys.stdout.write(answer.get("stdout", ""))
sys.exit(answer.get("code", 0))
"""


class Scripted:
    """A stand-in forgectl that answers ``show`` and otherwise what ``answer`` says (a JSON file
    beside it): exit code, stdout, stderr, and how long to sleep first."""

    def __init__(self, tmp_path: Path, mode: str = "run", **extra: str) -> None:
        self.directory = tmp_path / "scripted"
        self.directory.mkdir()
        self.path = self.directory / "forgectl"
        self.path.write_text(SCRIPTED.format(python=sys.executable), encoding="utf-8")
        self.path.chmod(0o700)
        self.answer()
        self.settings = settings_for(
            mode, tmp_path, PR_COMMENT_LIVE_FORGECTL=str(self.path), **extra
        )
        self.env = {"PATH": "/nonexistent", "GH_TOKEN": SENTINEL}

    def answer(self, **fields: Any) -> None:
        (self.directory / "answer.json").write_text(json.dumps(fields), encoding="utf-8")

    def runner(self, tmp_path: Path) -> Runner:
        runner = Runner(self.settings, tmp_path / "work", env=self.env)
        runner.verify_target(h.PR)
        return runner

    def write(self, runner: Runner, label: str = "w") -> Outcome:
        return runner.fg(
            label, "comment", "--json", h.PR, "--body-file", "-", stdin=b"x", write=True
        )


def _ledger_rows(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "report" / "ledger-testrun1.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_wait_for_audit_counts_only_records_after_the_count_taken_before_the_launch(
    tmp_path: Path,
) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env={"PATH": "/x"})
    runner.audit_path.parent.mkdir(parents=True)
    audit = runner.audit_path

    def add(result: str) -> None:
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"result": result}) + "\n")

    add("attempted")
    add("done")
    before = runner.audit_count("attempted")
    assert before == 1
    # the earlier launch's record is not this launch's: waiting on it must not return at once
    started = time.monotonic()
    assert runner.wait_for_audit("attempted", timeout=0.3, after=before) is False
    assert time.monotonic() - started >= 0.25
    add("attempted")
    assert runner.wait_for_audit("attempted", timeout=1, after=before) is True
    assert runner.wait_for_audit("attempted", timeout=0.1, after=before + 1) is False


def test_a_missing_cleanup_command_is_a_note_not_a_failure_and_the_scenario_error_survives(
    tmp_path: Path,
) -> None:
    cleanup = tmp_path / "cleanup.sh"
    cleanup.write_text("#!/bin/sh\n", encoding="utf-8")
    cleanup.chmod(0o700)
    scripted = Scripted(tmp_path, PR_COMMENT_LIVE_CLEANUP_CMD=str(cleanup))
    scripted.answer(stdout=json.dumps({"ids": ["gh:ic:IC_1"]}))

    def scenario(ctx: Ctx) -> None:
        scripted.write(ctx.runner)
        cleanup.unlink()  # gone by teardown
        ctx.check(False, "the host did something odd", "x.row")

    probe = Scenario("probe", scenario, ("x.row",), ("github",), True, (), "Fails.")
    with pytest.raises(LiveFailure, match="odd"):
        run_scenario(
            scripted.settings,
            probe,
            scripted.settings.targets["GITHUB"],
            tmp_path / "work",
            env=scripted.env,
        )
    report = json.loads(next((tmp_path / "report").glob("results-*-probe-github.json")).read_text())
    assert report["status"] == "failed: LiveFailure"
    assert any("cleanup command could not be started" in n for n in report["left_for_a_human"])


def test_a_cleanup_command_that_hangs_is_killed_and_noted(tmp_path: Path) -> None:
    cleanup = tmp_path / "cleanup.sh"
    cleanup.write_text(
        f"#!/bin/sh\n{shutil.which('sleep', path='/usr/bin:/bin')} 60\n", encoding="utf-8"
    )
    cleanup.chmod(0o700)
    scripted = Scripted(tmp_path, PR_COMMENT_LIVE_CLEANUP_CMD=str(cleanup))
    scripted.answer(stdout=json.dumps({"ids": ["gh:ic:IC_1"]}))
    runner = scripted.runner(tmp_path)
    runner.cleanup_timeout = 0.5
    scripted.write(runner)
    started = time.monotonic()
    notes = runner.teardown()
    assert time.monotonic() - started < 30
    assert any("timed out" in n for n in notes), notes


def test_a_step_with_a_placeholder_fails_in_run_mode_instead_of_being_deferred(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())
    with pytest.raises(LiveFailure, match="placeholder"):
        runner.pc("r", "resolve", h.PR, "--thread", "<thread_id>")
    assert forgectl.calls() == []


def test_a_step_that_did_not_run_is_not_a_pass_in_run_mode(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    runner = Runner(settings_for("run", tmp_path), tmp_path / "work", env=forgectl.environ())
    ctx = Ctx(runner, runner.settings.targets["GITHUB"], "t")
    with pytest.raises(LiveFailure, match="did not run"):
        ctx.want(Outcome(Step("t", "l", "pr-comment", [], True)), 0)
    plan = Ctx(Runner(PLAN, tmp_path / "w2", env={"PATH": "/x"}), PLAN_CASES[0][1], "t")
    plan.want(Outcome(Step("t", "l", "pr-comment", [], True)), 0)  # plan mode: nothing to want


def test_the_ledger_reads_landed_ids_from_an_error_envelope_on_any_exit(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    error = {
        "code": "outcome_partial",
        "landed": None,
        "details": {"landed": {"ids": ["gh:ic:IC_9"]}},
    }
    scripted.answer(code=12, stdout=json.dumps({"error": error}))
    runner = scripted.runner(tmp_path)
    out = scripted.write(runner)
    assert out.code == 12
    assert [r["id"] for r in runner.ledger] == ["gh:ic:IC_9"]
    assert [r["id"] for r in _ledger_rows(tmp_path) if "id" in r] == ["gh:ic:IC_9"]
    assert landed_ids({"error": {"landed": ["gh:ic:IC_8"]}}) == ["gh:ic:IC_8"]
    assert landed_ids({"ids": ["a"], "thread_ids": ["b"], "review_id": "c"}) == ["a", "b", "c"]
    assert landed_ids(None) == []


def test_ids_in_unparsable_output_are_ledgered_before_the_failure(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    scripted.answer(stdout="posted gh:ic:IC_77 then crashed")
    runner = scripted.runner(tmp_path)
    with pytest.raises(LiveFailure, match="not one JSON document"):
        scripted.write(runner)
    assert [r["id"] for r in runner.ledger] == ["gh:ic:IC_77"]
    assert runner.possible  # and the write is still listed as one whose outcome is unknown
    notes = runner.teardown()
    assert any("possibly posted" in n and "live-testrun1" in n for n in notes)


def test_a_real_write_is_listed_as_possibly_posted_before_it_ends_and_after_a_timeout(
    tmp_path: Path,
) -> None:
    scripted = Scripted(tmp_path)
    scripted.answer(sleep=60)
    runner = scripted.runner(tmp_path)
    started = time.monotonic()
    with pytest.raises(LiveFailure, match="no answer"):
        runner.fg(
            "slow",
            "comment",
            "--json",
            h.PR,
            "--body-file",
            "-",
            stdin=b"x",
            write=True,
            timeout=0.5,
        )
    assert time.monotonic() - started < 30  # the process group was killed, not waited for
    statuses = [r.get("status") for r in _ledger_rows(tmp_path)]
    assert statuses == ["possibly_posted"]
    assert len(runner.possible) == 1


def test_a_complete_answer_resolves_the_possibly_posted_entry(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    scripted.answer(stdout=json.dumps({"threads": []}))
    runner = scripted.runner(tmp_path)
    scripted.write(runner)
    assert runner.possible == []
    assert [r.get("status") for r in _ledger_rows(tmp_path)] == ["possibly_posted", "resolved"]
    scripted.answer(code=20, stdout=json.dumps({"error": {"code": "stale_head"}}))
    scripted.write(runner, "refused")
    assert runner.possible == []  # an exit that wrote nothing needs no entry either


def test_a_ledger_file_that_cannot_be_written_does_not_lose_the_rows(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    scripted.answer(stdout=json.dumps({"ids": ["gh:ic:IC_5"]}))
    runner = scripted.runner(tmp_path)
    (tmp_path / "report").mkdir(exist_ok=True)
    (tmp_path / "report" / "ledger-testrun1.jsonl").mkdir()  # appending to it fails
    scripted.write(runner)
    assert [r["id"] for r in runner.ledger] == ["gh:ic:IC_5"]
    assert any("ledger file could not be written" in n for n in runner.teardown())


def test_an_unwritable_report_directory_is_refused_before_any_call(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    broken = dataclasses.replace(scripted.settings, report_dir=tmp_path / "file" / "sub")
    scenario = Scenario("probe", lambda ctx: None, ("x.row",), ("github",), True, (), "Nothing.")
    with pytest.raises(LiveRefusal, match="REPORT_DIR"):
        run_scenario(
            broken, scenario, broken.targets["GITHUB"], tmp_path / "work", env=scripted.env
        )


def test_an_interrupt_mid_scenario_keeps_the_ledger_and_writes_the_report(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    scripted.answer(stdout=json.dumps({"ids": ["gh:ic:IC_3"]}))

    def interrupted(ctx: Ctx) -> None:
        scripted.write(ctx.runner)
        raise KeyboardInterrupt

    probe = Scenario("probe", interrupted, ("x.row",), ("github",), True, (), "Interrupted.")
    with pytest.raises(KeyboardInterrupt):
        run_scenario(
            scripted.settings,
            probe,
            scripted.settings.targets["GITHUB"],
            tmp_path / "work",
            env=scripted.env,
        )
    assert "gh:ic:IC_3" in [r.get("id") for r in _ledger_rows(tmp_path)]
    report = json.loads(next((tmp_path / "report").glob("results-*-probe-github.json")).read_text())
    assert report["status"] == "failed: KeyboardInterrupt"
    assert any("IC_3" in n for n in report["left_for_a_human"])


def test_a_secret_on_stderr_or_in_unparsable_stdout_reaches_no_message_note_or_report(
    tmp_path: Path,
) -> None:
    scripted = Scripted(tmp_path)

    def scenario(ctx: Ctx) -> None:
        scripted.answer(code=5, stdout="{}", stderr=f"boom {SENTINEL}")
        out = scripted.write(ctx.runner, "noisy")
        assert SENTINEL not in out.stderr and SENTINEL not in out.stdout
        ctx.observe("x.row", "stderr", out.stderr)
        scripted.answer(code=1, stdout=f"not json {SENTINEL}")
        try:
            scripted.write(ctx.runner, "garbage")
        except LiveFailure as exc:
            assert SENTINEL not in str(exc)
            ctx.observe("x.row", "message", str(exc))
        scripted.answer(code=5, stdout="{}", stderr=f"boom {SENTINEL}")
        ctx.want(scripted.write(ctx.runner, "again"), 0, row="x.row")  # fails, message built here

    probe = Scenario("probe", scenario, ("x.row",), ("github",), True, (), "Noisy.")
    with pytest.raises(LiveFailure) as caught:
        run_scenario(
            scripted.settings,
            probe,
            scripted.settings.targets["GITHUB"],
            tmp_path / "work",
            env=scripted.env,
        )
    assert SENTINEL not in str(caught.value) and "<GH_TOKEN>" in str(caught.value)
    results = next((tmp_path / "report").glob("results-*-probe-github.json")).read_text()
    assert SENTINEL not in results
    assert SENTINEL not in "".join(p.read_text() for p in (tmp_path / "report").iterdir())


def test_a_secret_with_characters_json_escapes_is_scrubbed_from_the_report(tmp_path: Path) -> None:
    secret = 'tok"en\\with\nnewline-12345'
    env = {"PATH": "/x", "GH_TOKEN": secret}

    def scenario(ctx: Ctx) -> None:
        ctx.observe("x.row", "value", f"seen {secret} here")
        ctx.observe("x.row", "nested", {"list": [secret], secret: 1})

    probe = Scenario("probe", scenario, ("x.row",), ("github",), True, (), "Observes.")
    settings = settings_for("plan", tmp_path)
    run_scenario(settings, probe, settings.targets["GITHUB"], tmp_path / "work", env=env)
    text = next((tmp_path / "report").glob("results-*-probe-github.json")).read_text()
    assert "newline-12345" not in text
    assert json.dumps(secret)[1:-1] not in text
    assert "<GH_TOKEN>" in text
    assert scrub({"a": [secret]}, env) == {"a": ["<GH_TOKEN>"]}
    assert redact(secret, env) == "<GH_TOKEN>"


# --- review round 1: the pty, the tap, the environment ---------------------------------------


def _run_python(tmp_path: Path, code: str, **kwargs: Any) -> Any:
    return run_on_pty(
        [sys.executable, "-c", code], env={}, cwd=tmp_path, answer=b"y\n", deadline=30, **kwargs
    )


def test_a_pty_child_that_prints_a_lot_to_stdout_does_not_hang_the_run(tmp_path: Path) -> None:
    code = "import json,sys; sys.stdout.write(json.dumps({'pad': 'x' * 400000}))"
    result = _run_python(tmp_path, code)
    assert result.code == 0 and result.document is not None
    assert len(result.document["pad"]) == 400000


def test_a_pty_child_that_prints_text_instead_of_json_fails_the_run(tmp_path: Path) -> None:
    with pytest.raises(LiveFailure, match="not one JSON document"):
        _run_python(tmp_path, "print('x' * 300000)")


def test_a_pty_prompt_is_found_in_the_tail_of_a_large_terminal_only() -> None:
    prompt = b"Post? [y/N] "
    assert _prompt_is_up(b"x" * 100_000 + b"\n" + prompt)
    assert not _prompt_is_up(prompt + b"\n" + b"x" * 100_000)  # long gone: not scanned
    assert not _prompt_is_up(b"x" * 100_000 + prompt)  # not at the start of a line
    assert _prompt_is_up(b"a\r\n" + prompt)


def test_the_tap_log_survives_a_half_written_last_line(tmp_path: Path) -> None:
    scripted = Scripted(tmp_path)
    runner = scripted.runner(tmp_path)
    runner.tap_path()
    log = tmp_path / "work" / "tap" / "calls.jsonl"
    log.write_text(
        json.dumps({"ts": 1, "argv": ["show"]}) + "\n" + '{"ts": 2, "ar', encoding="utf-8"
    )
    assert runner.tap_calls() == [["show"]]


def test_a_pid_file_is_read_only_once_its_line_is_complete(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    assert helpers.read_pidfile(pidfile, timeout=0.1) == 0
    pidfile.write_text("12", encoding="utf-8")  # half written: no newline yet
    assert helpers.read_pidfile(pidfile, timeout=0.1) == 0
    pidfile.write_text("1234\n", encoding="utf-8")
    assert helpers.read_pidfile(pidfile, timeout=0.1) == 1234
    pidfile.write_text("abc\n", encoding="utf-8")
    assert helpers.read_pidfile(pidfile, timeout=0.1) == 0


def test_children_get_a_private_home_unless_a_forgectl_config_is_set(tmp_path: Path) -> None:
    env = {"PATH": "/x", "HOME": "/home/real", "XDG_CONFIG_HOME": "/home/real/.config"}
    private = Runner(settings_for("plan", tmp_path), tmp_path / "a", env=env).child_env()
    assert private["HOME"] == str(tmp_path / "a" / "home")
    assert private["XDG_CONFIG_HOME"].startswith(str(tmp_path / "a" / "home"))
    config = tmp_path / "forgectl.yaml"
    config.write_text("hosts: {}\n", encoding="utf-8")
    config.chmod(0o600)
    chosen = settings_for("plan", tmp_path, PR_COMMENT_LIVE_FORGECTL_CONFIG=str(config))
    shared = Runner(chosen, tmp_path / "b", env=env).child_env()
    assert shared["HOME"] == "/home/real" and shared["XDG_CONFIG_HOME"] == "/home/real/.config"


def test_state_a_scenario_leaves_behind_is_listed_in_the_notes(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    runner = Runner(settings_for("plan", tmp_path), tmp_path / "work", env=forgectl.environ())
    runner.leave("a resolved thread")
    runner.leave("a resolved thread")
    assert runner.teardown() == ["left for a human: a resolved thread"]


# --- review round 1: plan mode, rows and scenarios -------------------------------------------


def test_a_plan_with_an_allowlist_and_no_target_shows_placeholder_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    NothingStarts(monkeypatch)
    settings = from_env({"PR_COMMENT_LIVE": "1", "PR_COMMENT_LIVE_ALLOW": "github.com/other/repo"})
    assert settings.refusals() == []
    for target in targets_for(settings):
        runner = (
            run_scenario(
                settings,
                SCENARIOS["comment_post"],
                target,
                tmp_path / target.name,
                env={"PATH": "/x"},
            )
            if target.backend == "github"
            else None
        )
        assert runner is None or {s.applied for s in runner.steps} == {"plan"}


def test_a_configured_target_is_checked_against_the_allowlist_even_in_plan_mode(
    tmp_path: Path,
) -> None:
    runner = Runner(settings_for("plan", tmp_path), tmp_path / "work", env={"PATH": "/x"})
    runner.verify_target(h.PR)
    with pytest.raises(LiveRefusal, match="ALLOW"):
        runner.verify_target("github.com/acme/other#1")
    with pytest.raises(LiveRefusal, match="ALLOW"):
        runner.verify_target("github.com/acme/widgets#1".replace("widgets", "elsewhere"))


def test_an_approve_write_needs_the_approve_variable_in_run_mode(tmp_path: Path) -> None:
    for approve, expected in (("", False), ("1", True)):
        extra = {"PR_COMMENT_LIVE_APPROVE": approve} if approve else {}
        settings = settings_for("run", tmp_path, **extra)
        runner = Runner(settings, tmp_path / f"w{approve}", env={"PATH": "/x"})
        ctx = Ctx(runner, settings.targets["GITHUB"], "t")
        assert ctx.may_approve is expected
    plan = Ctx(Runner(PLAN, tmp_path / "p", env={"PATH": "/x"}), PLAN_CASES[0][1], "t")
    assert plan.may_approve is True  # plan and preflight show the step; nothing is written


def _recorded_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: str
) -> list[list[str]]:
    settings = settings_for("run", tmp_path, **extra)
    runner = Runner(settings, tmp_path / "work", env={"PATH": "/x"})
    runner.verified.add(h.PR)
    calls: list[list[str]] = []

    def pc(label: str, *argv: str, **kw: Any) -> Outcome:
        calls.append([*argv])
        return Outcome(Step("s", label, "pr-comment", list(argv), False), 0, {}, real=True)

    def fg(label: str, *argv: str, **kw: Any) -> Outcome:
        doc = h.show_doc(h.PR)
        return Outcome(Step("s", label, "forgectl", list(argv), False), 0, doc, real=True)

    monkeypatch.setattr(runner, "pc", pc)
    monkeypatch.setattr(runner, "fg", fg)
    monkeypatch.setattr(runner, "allow_ref_text", lambda ref: None)
    monkeypatch.setattr(Ctx, "want", lambda *a, **k: None)
    monkeypatch.setattr(Ctx, "check", lambda *a, **k: None)
    SCENARIOS["refusals"].fn(Ctx(runner, settings.targets["GITHUB"], "refusals"))
    return calls


def test_refusal_probes_that_are_approve_writes_run_only_with_the_approve_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def real_approves(calls: list[list[str]]) -> list[list[str]]:
        return [c for c in calls if c[0] == "approve" and "--dry-run" not in c]

    assert real_approves(_recorded_refusals(tmp_path / "a", monkeypatch)) == []
    assert (
        len(
            real_approves(
                _recorded_refusals(tmp_path / "b", monkeypatch, PR_COMMENT_LIVE_APPROVE="1")
            )
        )
        == 2
    )


def test_the_closed_pr_scenario_writes_nothing_when_the_target_is_open(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    settings = settings_for("run", tmp_path, PR_COMMENT_LIVE_GITHUB_CLOSED_PR=h.PR)
    forgectl.script(_show(), _show(), _show())  # the target, the closed PR, its state: open
    with pytest.raises(LiveRefusal, match="nothing was written"):
        run_scenario(
            settings,
            SCENARIOS["closed_pr"],
            settings.targets["GITHUB"],
            tmp_path / "work",
            env=forgectl.environ(),
        )
    assert forgectl.writes() == []


def test_the_confirmation_probe_without_yes_is_a_write_for_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    NothingStarts(monkeypatch)
    target = PLAN_CASES[0][1]
    runner = run_scenario(
        PLAN,
        SCENARIOS["forgectl_confirmation_order"],
        target,
        tmp_path / "work",
        env={"PATH": "/nonexistent"},
    )
    (probe,) = [s for s in runner.steps if s.label == "write-no-yes"]
    assert probe.write and "--yes" not in probe.argv


def test_the_new_rows_are_there_and_say_what_they_do_not_probe() -> None:
    assert {"fk.diff_markers", "fk.since_grammar"} <= checklist.ROW_IDS
    rows = {row.id: row for row in checklist.ROWS}
    assert "pr-comment's own" in rows["th.not_found"].text
    assert "pr-comment's own" in rows["th.changed_false"].text
    assert "pr-comment's own marker placement" in rows["fk.gitlab_no_body_marker"].text
    assert "diff_markers" in str(checklist.automated("fk.diff_markers"))
    assert not checklist.automated("fk.since_grammar") and rows["fk.since_grammar"].manual


def test_the_document_says_never_to_show_locals_and_where_credentials_come_from() -> None:
    text = checklist.render()
    assert "--showlocals" in text and "environment variables" in text


def test_the_mention_probe_holds_the_unterminated_numeric_forms() -> None:
    from tests.live.scenarios import _mention_body

    body = _mention_body("someone", None)
    assert "&#64someone" in body and "&#x40someone" in body


def test_the_async_hook_sleeps_long_enough_that_returning_early_is_no_race() -> None:
    assert "sleep 20;" in async_hook_config(Path("/x/marker"))


@pytest.mark.parametrize(
    ("backticks", "tildes", "fence", "ok"),
    [
        (254, 0, "`" * 255 + "text", True),
        (254, 0, "`" * 256 + "text", False),  # over 255
        (255, 0, "~~~text", True),
        (300, 0, "`" * 255 + "text", False),  # a backtick fence a 255 run closes
        (0, 300, "```text", True),
        (255, 255, "`" * 255 + "text", True),
    ],
)
def test_the_fence_check_follows_the_documented_rules(
    backticks: int, tildes: int, fence: str, ok: bool
) -> None:
    split = helpers.RUN_SPLIT if backticks >= 255 and tildes >= 255 else ""
    body = f"A run.\n\n{fence}\nx {split}\n{fence[:-4]}\n"
    assert (helpers.fence_problems(body, backticks, tildes) == []) is ok


def test_the_run_split_marker_is_required_only_when_both_runs_reach_255() -> None:
    body = "A.\n\n~~~text\nx\n~~~\n"
    assert helpers.fence_problems(body, 255, 0) == []
    assert helpers.fence_problems(body.replace("x", "x <run split>"), 255, 0) != []
    assert helpers.text_fence("no block here") is None
