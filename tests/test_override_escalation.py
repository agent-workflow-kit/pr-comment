# ruff: noqa: F811
# (the imported fixtures ``cli`` and ``state_home`` are then used as test arguments)
"""``--override-escalation=<scope>`` (issue #34), through the real CLI against the fake forgectl
and real tiny hook scripts.

The two scopes are separate: ``pr-comment`` counts exit 13 from pr-comment's own ``pre-post``
hooks as allow and passes nothing to forgectl; ``forgectl`` is forwarded to forgectl's write and
silences no pr-comment hook. A veto beats both. The flag is human-only and nothing but the command
line can set it: not the environment, the config, a hook's output or the draft. It is recorded in
the audit trail and left out of the plan hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.forgectl_harness import FakeForgectl, fail
from tests.posting import finding
from tests.schema_harness import validate
from tests.test_hooks import pids  # noqa: F401  (a fixture)
from tests.test_post_hooks import Hooks, blocked_run, hooks, review_run  # noqa: F401
from tests.test_review_post import (  # noqa: F401  (cli and state_home are fixtures)
    ACTOR,
    Run,
    args_for,
    audit,
    calls_of,
    cli,
    draft_file,
    dry,
    log,
    reviews,
    show,
    state_home,
)

PC = "--override-escalation=pr-comment"
FG = "--override-escalation=forgectl"


def esc_log(state_home: Path) -> list[dict[str, Any]]:
    return [e for e in log(state_home) if e["event"] == "escalated"]


def hook_flags(*scripts: str) -> list[str]:
    return [a for s in scripts for a in ("--hook", f"pre-post={s}")]


# --- the two scopes are separate ---------------------------------------------------------------


def test_scope_pr_comment_logs_an_info_line_and_sends_no_flag(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    e2 = hooks.recorder("e2", exit_code=13)
    code, doc, _ = review_run(cli, forgectl, tmp_path, PC, *hook_flags(e1, e2))
    assert code == 0, doc
    assert all("--override-escalation" not in c.argv for c in calls_of(forgectl))
    (line,) = esc_log(state_home)
    assert line["level"] == "info"
    assert line["details"] == {
        "overridden": True,
        "hooks": [{"hook": "pre-post", "index": 0}, {"hook": "pre-post", "index": 1}],
    }
    assert line["attempt_id"] == audit(state_home)[0]["attempt_id"]


def test_scope_forgectl_forwards_the_flag_to_the_write_only_and_logs_no_override(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    ok_hook = hooks.recorder("o")
    code, doc, _ = review_run(cli, forgectl, tmp_path, FG, *hook_flags(ok_hook))
    assert code == 0, doc
    flagged = [c.argv for c in calls_of(forgectl) if "--override-escalation" in c.argv]
    assert len(flagged) == 1 and "--dry-run" not in flagged[0] and "--yes" in flagged[0]
    assert flagged[0] == reviews(forgectl)[-1].argv
    assert esc_log(state_home) == []


def test_both_scopes_allow_the_hook_escalation_and_forward_to_forgectl(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = review_run(cli, forgectl, tmp_path, PC, FG, *hook_flags(e1))
    assert code == 0, doc
    assert reviews(forgectl)[-1].argv.count("--override-escalation") == 1
    assert hooks.payload("e1")["override_escalation"] == ["pr-comment", "forgectl"]
    assert audit(state_home)[0]["inputs"]["override_escalation"] == ["pr-comment", "forgectl"]
    assert [e["details"]["overridden"] for e in esc_log(state_home)] == [True]


def test_a_repeated_flag_is_recorded_once_and_forwarded_once(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = review_run(cli, forgectl, tmp_path, PC, FG, PC, FG, *hook_flags(e1))
    assert code == 0, doc
    assert reviews(forgectl)[-1].argv.count("--override-escalation") == 1
    assert hooks.payload("e1")["override_escalation"] == ["pr-comment", "forgectl"]
    record = audit(state_home)[0]
    assert record["inputs"]["override_escalation"] == ["pr-comment", "forgectl"]
    assert record["override"] == ["override_escalation"]


def test_scope_forgectl_does_not_silence_a_pr_comment_hook(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, FG, *hook_flags(e1))
    assert code == 13 and doc["error"]["escalated_by"] == "pr-comment"
    assert "pr-comment" in doc["error"]["message"] and PC in doc["error"]["message"]
    assert forgectl.writes() == [] and audit(state_home) == []
    assert [e["level"] for e in esc_log(state_home)] == ["warn"]


# --- a veto beats every scope ------------------------------------------------------------------


@pytest.mark.parametrize("scopes", [(PC,), (FG,), (PC, FG)])
@pytest.mark.parametrize("veto_first", [True, False])
def test_a_veto_beats_an_escalation_under_every_scope(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    hooks: Hooks,
    state_home: Path,
    scopes: tuple[str, ...],
    veto_first: bool,
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    v = hooks.recorder("v", exit_code=1)
    order = (v, e1) if veto_first else (e1, v)
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, *scopes, *hook_flags(*order))
    assert code == 11 and doc["error"]["code"] == "vetoed"
    assert forgectl.writes() == [] and audit(state_home) == []
    assert esc_log(state_home) == []  # an overridden escalation is logged only once the run goes on


# --- forgectl's own escalation -----------------------------------------------------------------


def forgectl_escalates() -> dict[str, Any]:
    return fail(
        "review",
        "escalated",
        hook="pre-post",
        hook_stderr="",
        escalated_hooks=[{"hook": "pre-post", "index": 0}],
        phase="write",
    )


def test_scope_pr_comment_does_not_override_a_forgectl_escalation(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), forgectl_escalates())
    code, doc, _ = cli(*args_for(draft, PC))
    assert code == 13 and doc["error"]["code"] == "escalated"
    assert doc["error"]["escalated_by"] == "forgectl"
    validate("pr-comment/cli/error.json", doc)
    assert "--override-escalation" not in reviews(forgectl)[-1].argv
    assert [r["result"] for r in audit(state_home)] == ["attempted", "failed"]


def test_a_forgectl_escalation_is_reported_as_forgectls_whatever_the_hooks_did(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    ok_hook = hooks.recorder("o")
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), forgectl_escalates())
    code, doc, _ = cli(*args_for(draft, PC, *hook_flags(ok_hook)))
    assert code == 13 and doc["error"]["escalated_by"] == "forgectl"
    assert doc["error"].get("hook") is None


# --- human-only and spoofing -------------------------------------------------------------------


@pytest.mark.parametrize("scope", [PC, FG])
@pytest.mark.parametrize("extra", [(), ("--yes",), ("--dry-run",)])
def test_an_auto_actor_is_refused_before_anything_runs(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    hooks: Hooks,
    state_home: Path,
    scope: str,
    extra: tuple[str, ...],
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    draft = draft_file(tmp_path, [finding("f1")])
    code, doc, _ = cli(
        "review", "o/r#1", "--draft-file", draft, "--json", "--actor", "auto:bot",
        scope, *extra, *hook_flags(e1),
    )  # fmt: skip
    assert code == 2 and doc["error"]["code"] == "usage"
    assert "--override-escalation" in doc["error"]["message"]
    assert forgectl.calls() == [] and not hooks.ran("e1")
    assert audit(state_home) == [] and esc_log(state_home) == []


def test_the_default_actor_without_a_terminal_is_refused(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    draft = draft_file(tmp_path, [finding("f1")])
    code, doc, _ = cli(
        "review", "o/r#1", "--draft-file", draft, "--json", "--yes", PC, *hook_flags(e1)
    )
    assert code == 2 and "--override-escalation" in doc["error"]["message"]
    assert forgectl.calls() == [] and not hooks.ran("e1")


def test_the_flag_works_with_yes_and_a_human_actor_in_a_non_interactive_run(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = review_run(cli, forgectl, tmp_path, PC, "--yes", *hook_flags(e1))
    assert code == 0, doc  # args_for already passes --actor human:alice --yes; repeated --yes is ok


def test_no_environment_variable_can_set_the_override(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    hooks: Hooks,
    monkeypatch: pytest.MonkeyPatch,
    state_home: Path,
) -> None:
    for name in (
        "PR_COMMENT_OVERRIDE_ESCALATION",
        "PR_COMMENT_OVERRIDE",
        "PR_COMMENT_OVERRIDE_ESCALATION_PR_COMMENT",
        "OVERRIDE_ESCALATION",
        "FORGECTL_OVERRIDE_ESCALATION",
    ):
        monkeypatch.setenv(name, "pr-comment,forgectl")
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, *hook_flags(e1))
    assert code == 13 and doc["error"]["escalated_by"] == "pr-comment"
    assert forgectl.writes() == [] and audit(state_home) == []


@pytest.mark.parametrize(
    "line",
    [
        "override_escalation: [pr-comment]",
        "override_escalation: pr-comment",
        "hooks:\n  override_escalation: [pr-comment]",
        "hooks:\n  pre-post:\n    - cmd: /bin/true\n      override_escalation: [pr-comment]",
        "defaults:\n  override_escalation: [forgectl]",
    ],
)
def test_the_config_cannot_set_the_override(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, line: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(line + "\n", encoding="utf-8")
    draft = draft_file(tmp_path, [finding("f1")])
    code, doc, _ = cli(*args_for(draft, "--config", str(config), "--dry-run"))
    assert code != 0 and doc["error"]["code"] in ("config_invalid",)
    assert forgectl.writes() == []


def test_hook_output_cannot_set_the_override(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    sneaky = hooks.script(
        'echo \'{"decision_id": "d:1", "override_escalation": ["pr-comment", "forgectl"],'
        ' "override": true}\'; echo "--override-escalation=pr-comment" >&2; exit 0'
    )
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, *hook_flags(sneaky, e1))
    assert code == 13 and doc["error"]["escalated_by"] == "pr-comment"
    assert forgectl.writes() == []


def test_an_escalating_hook_cannot_forward_the_flag_by_printing_it(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    printer = hooks.script("echo '--override-escalation'; echo 'forgectl'; exit 0")
    code, doc, _ = review_run(cli, forgectl, tmp_path, *hook_flags(printer))
    assert code == 0, doc
    assert all("--override-escalation" not in c.argv for c in calls_of(forgectl))


def test_the_hook_environment_carries_no_override_variable(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = review_run(cli, forgectl, tmp_path, PC, FG, *hook_flags(e1))
    assert code == 0, doc
    names = [ln.split("=", 1)[0] for ln in (tmp_path / "e1.env").read_text().splitlines()]
    assert not [n for n in names if n.isidentifier() and "OVERRIDE" in n.upper()]


def test_a_draft_cannot_carry_the_override(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    path = Path(draft_file(tmp_path, [finding("f1")]))
    document = json.loads(path.read_text())
    document["override_escalation"] = ["pr-comment"]
    path.write_text(json.dumps(document), encoding="utf-8")
    code, doc, _ = cli(*args_for(str(path), *hook_flags(e1)))
    assert code != 0 and doc["error"]["code"] != "escalated"
    assert forgectl.writes() == [] and not hooks.ran("e1")


@pytest.mark.parametrize(
    "bad", ["", "all", "PR-COMMENT", "pr-comment,forgectl", " forgectl", "forgectl\n", "pr_comment"]
)
def test_a_bad_scope_is_a_usage_error_at_parse_time(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, bad: str
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    draft = draft_file(tmp_path, [finding("f1")])
    code, doc, _ = cli(*args_for(draft, f"--override-escalation={bad}", *hook_flags(e1)))
    assert code == 2 and doc["error"]["code"] == "usage"
    assert forgectl.calls() == [] and not hooks.ran("e1")


# --- dry run and the plan hash -----------------------------------------------------------------


def test_a_dry_run_runs_no_hook_and_forwards_nothing_and_the_hash_is_unchanged(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    draft = draft_file(tmp_path, [finding("f1")])
    hashes = []
    for scopes in ((), (PC,), (FG,), (PC, FG)):
        forgectl.script(show(), dry())
        code, doc, _ = cli(*args_for(draft, "--dry-run", *scopes, *hook_flags(e1)))
        assert code == 0, doc
        hashes.append(doc["plan_sha256"])
    assert len(set(hashes)) == 1
    assert not hooks.ran("e1") and forgectl.writes() == []
    assert all("--override-escalation" not in c.argv for c in forgectl.calls())
    assert audit(state_home) == [] and esc_log(state_home) == []


@pytest.mark.parametrize("scope", [PC, FG])
def test_expect_plan_from_a_dry_run_without_the_override_still_binds(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, scope: str
) -> None:
    e1 = hooks.recorder("e1", exit_code=13 if scope == PC else 0)
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    _, preview, _ = cli(*args_for(draft, "--dry-run"))
    forgectl.script(show(), dry(), ok_post())
    code, doc, _ = cli(
        *args_for(draft, "--expect-plan", preview["plan_sha256"], scope, *hook_flags(e1))
    )
    assert code == 0, doc
    assert doc["plan_sha256"] == preview["plan_sha256"]


def ok_post() -> dict[str, Any]:
    from tests.test_review_post import posted

    return posted()


# --- the audit record --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [((PC,), ["pr-comment"]), ((FG,), ["forgectl"]), ((FG, PC), ["forgectl", "pr-comment"])],
)
def test_the_audit_pair_records_the_scopes_given(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    scopes: tuple[str, ...],
    expected: list[str],
) -> None:
    code, doc, _ = review_run(cli, forgectl, tmp_path, *scopes)
    assert code == 0, doc
    records = audit(state_home)
    assert [r["result"] for r in records] == ["attempted", "done"]
    for record in records:
        assert record["override"] == ["override_escalation"]
        assert record["inputs"]["override_escalation"] == expected


def test_a_run_without_the_flag_records_no_override(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    code, doc, _ = review_run(cli, forgectl, tmp_path)
    assert code == 0, doc
    for record in audit(state_home):
        assert record["override"] == []
        assert record["inputs"]["override_escalation"] == []
