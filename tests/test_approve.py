"""The approve order and the automatic approval guard (issue #31): ``approve`` and ``review
--approve`` through the real CLI against the fake forgectl process (``tests/fakebin/forgectl``,
pinned to forgectl's own schemas).

An approval by an agent bypasses a human's review, so the guard is tested as a table: every missing
condition gives its exit, before the dialog, before any pre-check and before any write. Every test
runs with a private HOME and state directory; no real forgectl is ever called.
"""

from __future__ import annotations

import dataclasses
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

from pr_comment import approve as approve_module
from pr_comment import cli as cli_module
from pr_comment import prepare as prepare_module
from pr_comment.cli import main
from pr_comment.config import LoadedConfig
from pr_comment.output import Outcome
from tests import forgectl_harness as h
from tests.forgectl_harness import (
    FakeForgectl,
    ProcessCall,
    assert_edge_rules,
    fail,
    ok,
    refuse,
)
from tests.posting import PR, A, B, draft_doc, dry_reply, finding
from tests.schema_harness import validate
from tests.test_prepare import request, run, show_reply

Run = Callable[..., tuple[int, Any, str]]

GL_PR = h.GITLAB_PR
HUMAN = "human:alice"
AUTO = "auto:policy"
REPO = "github.com/acme/widgets"
GATE = "gate-1"


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


def dry_approve(pr: str = PR, **kw: Any) -> dict[str, Any]:
    return ok("approve", h.post_doc("approve", pr, dry_run=True, **kw))


def approved(pr: str = PR, **kw: Any) -> dict[str, Any]:
    kw.setdefault("ids", ["gh:r:R_1"])
    kw.setdefault("review_id", "gh:r:R_1")
    return ok("approve", h.post_doc("approve", pr, **kw))


def dry_review(pr: str = PR, **kw: Any) -> dict[str, Any]:
    return ok("review", h.post_doc("review", pr, dry_run=True, **kw))


def reviewed(pr: str = PR, **kw: Any) -> dict[str, Any]:
    kw.setdefault("ids", ["gh:rc:RC_1", "gh:r:R_1"])
    kw.setdefault("review_id", "gh:r:R_1")
    return ok("review", h.post_doc("review", pr, **kw))


def pins(head: str = A, base: str = "main") -> tuple[str, ...]:
    return ("--expected-head", head, "--expected-base", base)


def human_approve(pr: str = PR, *extra: str) -> tuple[str, ...]:
    return ("approve", pr, "--json", "--actor", HUMAN, "--yes", *pins(), *extra)


def auto_approve(config: Path | None, pr: str = PR, *extra: str) -> tuple[str, ...]:
    cfg = () if config is None else ("--config", str(config))
    return (
        "approve",
        pr,
        "--json",
        "--actor",
        AUTO,
        "--yes",
        *cfg,
        *pins(),
        "--decision-id",
        GATE,
        *extra,
    )


def write_config(tmp_path: Path, text: str, name: str = "config.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def allowing(tmp_path: Path) -> Path:
    return write_config(tmp_path, "allow_auto_approve: true\n")


def draft_file(tmp_path: Path, findings: list[dict[str, Any]], **kw: Any) -> str:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft_doc(findings, **kw)), encoding="utf-8")
    return str(path)


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


def subcommands(forgectl: FakeForgectl) -> list[str]:
    """The calls but the ``--version`` read (the lower version, read once per write)."""
    return [c.subcommand for c in forgectl.calls() if c.subcommand != "--version"]


def flag(call: ProcessCall, name: str) -> list[str]:
    return [a.split("=", 1)[1] for a in call.argv if a.startswith(name + "=")]


def validate_audit(records: list[dict[str, Any]]) -> None:
    for record in records:
        validate("pr-comment/audit.json", record)


def nothing_sent(forgectl: FakeForgectl, state_home: Path, calls: list[str]) -> None:
    """The run ended at its refusal: only ``calls`` reached forgectl, no write and no audit
    record."""
    assert subcommands(forgectl) == calls
    assert forgectl.writes() == []
    assert audit(state_home) == []


# --- approve: a human ------------------------------------------------------------------------


def test_a_confirmed_approve_posts_the_plan_byte_for_byte_and_audits_the_pair(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    body = tmp_path / "body.md"
    body.write_text("Looks good to me.\n<b>ok</b> @amy\n", encoding="utf-8")
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*human_approve(PR, "--body-file", str(body), "--decision-id", "d1"))
    assert code == 0, doc
    validate("pr-comment/cli/approve.json", doc)
    assert doc["event"] == "approve" and doc["dry_run"] is False and doc["plan"] is None
    assert doc["ids"] == ["gh:r:R_1"] and doc["review_id"] == "gh:r:R_1"
    assert doc["already_posted"] is False and doc["head_verified"] is None
    assert doc["head_sha"] == A and doc["base_ref"] == "main"
    assert doc["mentions_neutralized"] == ["amy"]
    assert doc["dedupe_key"].startswith("prc:approve:")

    assert subcommands(forgectl) == ["show", "approve", "approve"]
    pre, real = (c for c in forgectl.calls() if c.subcommand == "approve")
    assert pre.is_dry_run and real.is_write
    assert real.stdin == pre.stdin and real.stdin is not None
    text = real.stdin.decode()
    assert text.endswith("<!-- pr-comment:v1 -->")
    assert "Looks good to me." in text and "＜b>ok" in text and "＠amy" in text
    assert flag(real, "--expected-head") == [A] and flag(real, "--expected-base") == ["main"]
    assert flag(real, "--dedupe-key") == [doc["dedupe_key"]]
    assert flag(real, "--decision-id") == ["d1"] and flag(real, "--actor") == [HUMAN]
    assert "--yes" in real.argv and "--dry-run" not in real.argv
    assert (
        real.argv.count("--body-file") == 1 and real.argv[real.argv.index("--body-file") + 1] == "-"
    )
    assert_edge_rules(
        forgectl.calls(),
        root=tmp_path,
        texts=[text],
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
    assert attempted["action"] == "approve" and attempted["target"] == PR
    assert attempted["decision_id"] == ["d1"] and attempted["actor"] == HUMAN
    assert outcome["inputs"]["event"] == "approve" and outcome["inputs"]["draft_id"] is None
    assert outcome["inputs"]["pinned_head"] == A and outcome["inputs"]["base_ref"] == "main"
    assert outcome["inputs"]["plan_sha256"] == doc["plan_sha256"]
    (write,) = outcome["writes"]
    assert (write["index"], write["action"], write["result"]) == (0, "approve", "done")
    assert write["lower"]["review_id"] == "gh:r:R_1"
    raw = (state_dir(state_home) / "audit.jsonl").read_text()
    assert "Looks good" not in raw  # hashes only, never the body


def test_an_approve_without_a_body_posts_the_attribution_line_and_the_marker(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, golden: Callable[[str, str], None]
) -> None:
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*human_approve())
    assert code == 0, doc
    real = forgectl.writes()[0]
    assert real.stdin is not None
    text = real.stdin.decode()
    attribution, blank, marker = text.split("\n")
    assert attribution.startswith("Drafted with AI assistance · posted by `alice` after review")
    assert blank == "" and marker == "<!-- pr-comment:v1 -->"
    golden("approve/no-body.txt", text.replace(A[:12], "<sha12>").replace("0.1.", "<v>."))
    assert doc["dedupe_key"].startswith("prc:approve:")


def test_a_blank_body_file_is_the_same_approval_as_no_body_file(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    blank = tmp_path / "blank.md"
    blank.write_text(" \n\t\r\n", encoding="utf-8")
    forgectl.script(show(), dry_approve(), show(), dry_approve())
    code, with_blank, _ = cli(*human_approve(PR, "--body-file", str(blank), "--dry-run"))
    assert code == 0
    code, without, _ = cli(*human_approve(PR, "--dry-run"))
    assert code == 0
    assert with_blank["plan_sha256"] == without["plan_sha256"]
    assert with_blank["dedupe_key"] == without["dedupe_key"]


def test_the_body_of_an_approve_can_come_from_stdin(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*human_approve(PR, "--body-file", "-"), stdin=b"ship it")
    assert code == 0, doc
    real = forgectl.writes()[0]
    assert real.stdin is not None and b"ship it" in real.stdin


def test_the_dry_run_of_an_approve_sends_nothing_and_validates(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    golden_json: Callable[[str, Any], None],
) -> None:
    forgectl.script(show(), dry_approve())
    code, doc, _ = cli(*human_approve(PR, "--dry-run"))
    assert code == 0, doc
    validate("pr-comment/cli/approve.json", doc)
    assert doc["dry_run"] is True and doc["plan"]["commands"][0][1] == "approve"
    assert forgectl.writes() == [] and audit(state_home) == []
    command = doc["plan"]["commands"][0]
    assert "--yes" in command and f"--expected-head={A}" in command
    golden_json("approve/dry-run.json", _stable({**doc, "plan": None}))


_VOLATILE = {
    "ts": "<ts>",
    "attempt_id": "<attempt>",
    "version": "<version>",
    "plan_sha256": "<plan>",
    "sha256": "<sha256>",
    "bytes": "<bytes>",
    "config_hash": "<config>",
    "config_path": None,
    "dedupe_key": "<key>",
}


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_VOLATILE[k] if k in _VOLATILE and v is not None else _stable(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def test_golden_approve_result_and_audit_pair(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    golden_json: Callable[[str, Any], None],
) -> None:
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*human_approve())
    assert code == 0
    validate("pr-comment/cli/approve.json", doc)
    golden_json("approve/result.json", _stable(doc))
    records = audit(state_home)
    validate_audit(records)
    golden_json("approve/audit-pair.json", _stable(records))


def test_already_posted_is_success_and_recorded_as_such(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(show(), dry_approve(already_posted=True), approved(already_posted=True))
    code, doc, _ = cli(*human_approve())
    assert code == 0 and doc["already_posted"] is True
    validate("pr-comment/cli/approve.json", doc)
    assert audit(state_home)[1]["result"] == "already_posted"


def test_head_verified_false_is_reported_in_json_and_in_the_human_output(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(show(), dry_approve(), approved(head_verified=False))
    code, doc, err = cli(*human_approve())
    assert code == 0 and doc["head_verified"] is False
    validate("pr-comment/cli/approve.json", doc)
    assert "head_verified" in err or "cannot confirm" in err


def test_a_gitlab_approve_reports_no_review_id(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"),
        dry_approve(GL_PR),
        approved(GL_PR, ids=["gl:n:11"], review_id=None, thread_ids=[]),
    )
    code, doc, _ = cli(*human_approve(GL_PR))
    assert code == 0, doc
    validate("pr-comment/cli/approve.json", doc)
    assert doc["review_id"] is None and doc["ids"] == ["gl:n:11"]
    assert doc["warnings"] == []  # an approve has no body thread (only a review does)


def test_a_gitlab_rerun_that_completes_the_approval_reports_follow_up_done(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"),
        dry_approve(GL_PR, already_posted=True),
        approved(GL_PR, ids=["gl:n:11"], review_id=None, already_posted=True, follow_up_done=True),
    )
    code, doc, _ = cli(*human_approve(GL_PR))
    assert code == 0, doc
    validate("pr-comment/cli/approve.json", doc)
    assert doc["already_posted"] is True and doc["follow_up_done"] is True


# --- approve: refusals and failures of forgectl ----------------------------------------------


def test_approved_stale_head_is_exit_12_landed_and_not_retried(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(
        show(),
        dry_approve(),
        fail("approve", "approved_stale_head", details={"ids": ["gh:r:R_1"]}, phase="write"),
    )
    code, doc, _ = cli(*human_approve())
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "approved_stale_head"
    assert (error["landed"], error["retry"]) == (True, "none")
    assert len(forgectl.writes()) == 1
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "partial"]


def test_github_outcome_partial_needs_a_new_key(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    details = {"landed": {"ids": [], "thread_ids": []}, "failed": "approve"}
    forgectl.script(show(), dry_approve(), fail("approve", "outcome_partial", details=details))
    code, doc, _ = cli(*human_approve())
    assert code == 12
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "new_key")
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize(
    ("landed", "want"),
    [
        ({"ids": ["gl:n:5"], "thread_ids": []}, True),
        ({"ids": [], "thread_ids": []}, False),
    ],
)
def test_gitlab_outcome_partial_is_the_note_without_the_approval(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    landed: dict[str, Any],
    want: bool,
) -> None:
    """A GitLab approval with a body is a note, then the approval: a failure between them keeps
    the key, and a rerun completes only the approval."""
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"),
        dry_approve(GL_PR),
        fail("approve", "outcome_partial", details={"landed": landed, "failed": "approve"}),
    )
    code, doc, _ = cli(*human_approve(GL_PR))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_partial"
    assert (error["landed"], error["retry"]) == (want, "same_command")
    assert audit(state_home)[1]["result"] == "partial"
    assert len(forgectl.writes()) == 1


REFUSAL_DETAILS: dict[str, dict[str, Any]] = {
    "stale_head": {"head_sha": B},  # a push between the pre-check and the write
    "pending_review_exists": {},
}


@pytest.mark.parametrize("reason", sorted(REFUSAL_DETAILS))
def test_a_refusal_of_the_real_call_passes_through_as_exit_20(
    cli: Run, forgectl: FakeForgectl, state_home: Path, reason: str
) -> None:
    forgectl.script(
        show(),
        dry_approve(),
        refuse("approve", reason, details=REFUSAL_DETAILS[reason], phase="write"),
    )
    code, doc, _ = cli(*human_approve())
    assert code == 20
    validate("pr-comment/cli/error.json", doc)
    assert doc["error"]["reason"] == reason and doc["error"]["lower_error"]["reason"] == reason
    assert [r["result"] for r in audit(state_home)] == ["attempted", "refused"]
    assert len(forgectl.writes()) == 1


def test_a_refusal_of_the_pre_check_ends_the_run_before_the_dialog(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(show(), refuse("approve", "self_approval", details={"basis": "authenticated"}))
    code, doc, _ = cli(*human_approve())
    assert code == 20 and doc["error"]["reason"] == "self_approval"
    nothing_sent(forgectl, state_home, ["show", "approve"])


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_a_closed_or_merged_pr_is_not_approved(
    cli: Run, forgectl: FakeForgectl, state_home: Path, state: str
) -> None:
    doc = h.show_doc(PR, state=state)
    doc["closed_at"] = "2026-01-03T00:00:00Z"
    if state == "merged":
        doc.update(
            merged_at="2026-01-03T00:00:00Z",
            merged_by="octo",
            merged_head_sha=A,
            merge_commit_sha="d" * 40,
        )
    forgectl.script(ok("show", doc))
    code, error, _ = cli(*human_approve())
    assert code == 20 and error["error"]["reason"] == "not_open"
    nothing_sent(forgectl, state_home, ["show"])


def test_a_stale_head_or_base_is_refused_before_the_pre_check(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(show(head=B), show())
    code, error, _ = cli("approve", PR, "--json", "--actor", HUMAN, "--yes", *pins())
    assert code == 20 and error["error"]["reason"] == "stale_head"
    assert error["error"]["details"] == {"head_sha": B}
    code, error, _ = cli("approve", PR, "--json", "--actor", HUMAN, "--yes", *pins(base="dev"))
    assert code == 20 and error["error"]["reason"] == "stale_base"
    nothing_sent(forgectl, state_home, ["show", "show"])


def test_outside_an_interactive_run_both_pins_are_required(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    for extra in ((), ("--expected-head", A), ("--expected-base", "main")):
        code, error, _ = cli("approve", PR, "--json", "--actor", HUMAN, "--yes", *extra)
        assert code == 2 and error["error"]["code"] == "usage"
    nothing_sent(forgectl, state_home, [])


# --- approve: the dialog ---------------------------------------------------------------------


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], list[Any]]:
    """Make the run interactive; the dialog answers ``yes`` or ``no`` and records what it was
    shown (the dialog itself is tested in test_confirm*.py)."""
    shown: list[Any] = []

    def setup(answer: bool) -> list[Any]:
        monkeypatch.setattr(cli_module, "stream_is_tty", lambda stream: True)

        def confirm(prepared: Any, **kw: Any) -> bool:
            shown.append(prepared)
            return answer

        monkeypatch.setattr(cli_module, "confirm", confirm)
        return shown

    return setup


def test_an_interactive_approve_pins_head_and_base_from_show_and_posts_what_was_shown(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    interactive: Callable[[bool], list[Any]],
) -> None:
    forgectl.script(show(head=B), dry_approve(head_sha=B), approved(head_sha=B))
    shown = interactive(True)
    code, doc, _ = cli("approve", PR, "--json", "--actor", HUMAN)
    assert code == 0, doc
    assert doc["head_sha"] == B and doc["base_ref"] == "main"
    (prepared,) = shown
    real = forgectl.writes()[0]
    assert real.stdin == prepared.plan.input  # the bytes the human confirmed
    assert flag(real, "--expected-head") == [B] and flag(real, "--expected-base") == ["main"]
    assert [r["result"] for r in audit(state_home)] == ["attempted", "done"]


def test_a_declined_approve_dialog_posts_nothing_and_records_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    interactive: Callable[[bool], list[Any]],
) -> None:
    forgectl.script(show(), dry_approve())
    shown = interactive(False)
    code, doc, _ = cli("approve", PR, "--json", "--actor", HUMAN)
    assert code == 13 and doc["error"]["message"] == "declined" and len(shown) == 1
    assert forgectl.writes() == [] and audit(state_home) == []


def test_a_non_interactive_approve_without_yes_posts_nothing(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    forgectl.script(show(), dry_approve())
    code, doc, _ = cli("approve", PR, "--json", "--actor", HUMAN, *pins())
    assert code == 13 and doc["error"]["code"] == "confirmation_required"
    assert forgectl.writes() == [] and audit(state_home) == []


# --- the guard: an automatic approve ---------------------------------------------------------


def test_an_automatic_approve_posts_when_the_guard_is_satisfied(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*auto_approve(allowing(tmp_path)))
    assert code == 0, doc
    validate("pr-comment/cli/approve.json", doc)
    real = forgectl.writes()[0]
    assert flag(real, "--actor") == [AUTO] and flag(real, "--decision-id") == [GATE]
    assert flag(real, "--expected-head") == [A] and flag(real, "--expected-base") == ["main"]
    assert real.stdin is not None
    assert real.stdin.decode().startswith("Posted automatically under policy `policy`")
    records = audit(state_home)
    validate_audit(records)
    assert records[0]["actor"] == AUTO and records[0]["decision_id"] == [GATE]
    assert records[0]["actor_source"] == "flag"


def test_an_automatic_approve_is_allowed_per_repo_and_only_for_that_repo(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    config = write_config(
        tmp_path, f"allow_auto_approve: false\nrepos:\n  {REPO}:\n    allow_auto_approve: true\n"
    )
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*auto_approve(config))
    assert code == 0, doc

    other = "github.com/acme/other#9"
    forgectl.script(show(pr=other))
    code, error, _ = cli(*auto_approve(config, other))
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"


def test_a_repo_can_switch_the_global_opt_in_off(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    config = write_config(
        tmp_path, f"allow_auto_approve: true\nrepos:\n  {REPO}:\n    allow_auto_approve: false\n"
    )
    forgectl.script(show())
    code, error, _ = cli(*auto_approve(config))
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    nothing_sent(forgectl, state_home, ["show"])


def test_the_repo_key_match_ignores_ascii_case_only(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    config = write_config(
        tmp_path, "repos:\n  github.com/ACME/Widgets:\n    allow_auto_approve: true\n"
    )
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*auto_approve(config))
    assert code == 0, doc


@pytest.mark.parametrize(
    ("case", "extra_flags", "config", "head", "base", "want_exit", "want_reason"),
    [
        ("disabled", (), False, A, "main", 20, "auto_approve_disabled"),
        ("no decision id", ("--no-decision",), True, A, "main", 20, "auto_approve_disabled"),
        ("no expected head", ("--no-head",), True, A, "main", 2, None),
        ("no expected base", ("--no-base",), True, A, "main", 2, None),
        ("stale head", (), True, B, "main", 20, "stale_head"),
        ("stale base", (), True, A, "dev", 20, "stale_base"),
    ],
)
def test_the_guard_table_each_missing_condition_gives_its_exit_and_sends_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    case: str,
    extra_flags: tuple[str, ...],
    config: bool,
    head: str,
    base: str,
    want_exit: int,
    want_reason: str | None,
) -> None:
    cfg = allowing(tmp_path) if config else write_config(tmp_path, "{}\n")
    argv = ["approve", PR, "--json", "--actor", AUTO, "--yes", "--config", str(cfg)]
    if "--no-head" not in extra_flags:
        argv += ["--expected-head", head]
    if "--no-base" not in extra_flags:
        argv += ["--expected-base", base]
    if "--no-decision" not in extra_flags:
        argv += ["--decision-id", GATE]
    if want_exit != 2:  # a usage error ends the run before show
        forgectl.script(show())
    code, error, _ = cli(*argv)
    assert code == want_exit, (case, error)
    validate("pr-comment/cli/error.json", error)
    assert error["error"]["reason"] == want_reason
    if want_exit == 20:
        assert error["error"]["code"] == "refused"
    # nothing past show (a usage error not even that): no pre-check, no dialog, no write, no
    # audit record
    assert subcommands(forgectl) == ([] if want_exit == 2 else ["show"])
    assert forgectl.writes() == [] and audit(state_home) == []


@pytest.mark.parametrize("yes", [True, False])
def test_yes_never_bypasses_the_guard_and_the_guard_comes_before_the_dialog(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    interactive_guard: Callable[[], list[Any]],
    yes: bool,
) -> None:
    asked = interactive_guard()
    cfg = write_config(tmp_path, "{}\n")
    argv = ["approve", PR, "--json", "--actor", AUTO, "--config", str(cfg), *pins()]
    argv += ["--decision-id", GATE] + (["--yes"] if yes else [])
    forgectl.script(show())
    code, error, _ = cli(*argv)
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    assert asked == []  # no dialog was shown for a refused approval
    nothing_sent(forgectl, state_home, ["show"])


@pytest.fixture
def interactive_guard(monkeypatch: pytest.MonkeyPatch) -> Callable[[], list[Any]]:
    def setup() -> list[Any]:
        asked: list[Any] = []

        def confirm(prepared: Any, **kw: Any) -> bool:
            asked.append(prepared)
            return True

        monkeypatch.setattr(cli_module, "confirm", confirm)
        return asked

    return setup


def test_the_dry_run_applies_the_guard_too(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    cfg = write_config(tmp_path, "{}\n")
    forgectl.script(show())
    code, error, _ = cli(*auto_approve(cfg, PR, "--dry-run"))
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    nothing_sent(forgectl, state_home, ["show"])


def test_the_default_actor_of_a_non_tty_run_is_automatic_and_guarded(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    forgectl.script(show())
    code, error, _ = cli("approve", PR, "--json", "--yes", *pins(), "--decision-id", GATE)
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    nothing_sent(forgectl, state_home, ["show"])
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(
        "approve",
        PR,
        "--json",
        "--yes",
        "--config",
        str(allowing(tmp_path)),
        *pins(),
        "--decision-id",
        GATE,
    )
    assert code == 0, doc
    assert audit(state_home)[0]["actor"] == "auto:unknown"
    assert audit(state_home)[0]["actor_source"] == "default"


def test_the_environment_cannot_enable_automatic_approval(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PR_COMMENT_ALLOW_AUTO_APPROVE", "PR_COMMENT_ALLOW__AUTO__APPROVE"):
        monkeypatch.setenv(name, "true")
    cfg = write_config(tmp_path, "{}\n")
    forgectl.script(show())
    code, error, _ = cli(*auto_approve(cfg))
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    nothing_sent(forgectl, state_home, ["show"])


def test_a_config_in_the_working_directory_is_never_read(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "config.yaml").write_text("allow_auto_approve: true\n")
    (checkout / ".pr-comment.yaml").write_text("allow_auto_approve: true\n")
    monkeypatch.chdir(checkout)
    forgectl.script(show())
    code, error, _ = cli(*auto_approve(None))
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"


@pytest.mark.parametrize(
    "extra",
    [
        ("--override-escalation=forgectl",),
        ("--override-escalation", "pr-comment"),
        ("--allow-mention", "amy"),
    ],
)
def test_an_automatic_actor_cannot_use_the_human_only_flags(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, extra: tuple[str, ...]
) -> None:
    code, error, _ = cli(*auto_approve(allowing(tmp_path), PR, *extra))
    assert code == 2 and error["error"]["code"] == "usage"
    nothing_sent(forgectl, state_home, [])


@pytest.mark.parametrize(
    "actor", ["AUTO:policy", "Auto:policy", " auto:policy", "auto: policy", "agent"]
)
def test_a_malformed_actor_is_a_usage_error_not_a_human(
    cli: Run, forgectl: FakeForgectl, state_home: Path, actor: str
) -> None:
    code, error, _ = cli("approve", PR, "--json", "--actor", actor, "--yes", *pins())
    assert code == 2 and error["error"]["code"] == "usage"
    nothing_sent(forgectl, state_home, [])


def test_a_human_actor_claim_is_not_subject_to_the_guard_which_is_advisory(
    cli: Run, forgectl: FakeForgectl, state_home: Path
) -> None:
    """docs/interface.md: human-only rules are advisory against a same-user process that claims
    ``--actor human:<user>``; the record shows how the actor was obtained."""
    forgectl.script(show(), dry_approve(), approved())
    code, doc, _ = cli(*human_approve())
    assert code == 0, doc
    assert audit(state_home)[0]["actor_source"] == "flag"


def test_guard_checks_only_the_callers_decision_ids() -> None:
    """Ids a hook prints never satisfy the guard: it runs before any hook and takes only the
    caller's ``--decision-id`` values."""
    outcome = approve_module.check_auto_approve(
        actor=AUTO,
        approving=True,
        allow=True,
        decision_ids=(),
        expected_head=A,
        pinned_head=A,
        anchor_head=None,
    )
    assert outcome is not None and outcome.exit_code == 20


# --- the guard: the unit table ---------------------------------------------------------------


def _check(**override: Any) -> Outcome | None:
    args: dict[str, Any] = {
        "actor": AUTO,
        "approving": True,
        "allow": True,
        "decision_ids": (GATE,),
        "expected_head": A,
        "pinned_head": A,
        "anchor_head": A,
    }
    args.update(override)
    return approve_module.check_auto_approve(**args)


def _reason(outcome: Outcome | None) -> tuple[int, str | None]:
    assert outcome is not None and outcome.document is not None
    error = outcome.document["error"]
    return outcome.exit_code, error.get("reason")


def test_the_unit_guard_allows_when_every_condition_holds() -> None:
    assert _check() is None
    assert _check(anchor_head=None) is None


@pytest.mark.parametrize("actor", ["human:alice", "human:unknown"])
def test_the_unit_guard_never_applies_to_a_human(actor: str) -> None:
    assert _check(actor=actor, allow=False, decision_ids=(), expected_head=None) is None


def test_the_unit_guard_does_not_apply_to_what_is_not_an_approval() -> None:
    assert _check(approving=False, allow=False) is None


@pytest.mark.parametrize(
    ("override", "want"),
    [
        ({"allow": False}, (20, "auto_approve_disabled")),
        ({"decision_ids": ()}, (20, "auto_approve_disabled")),
        ({"expected_head": None}, (2, None)),
        ({"expected_head": B}, (20, "stale_head")),
        ({"pinned_head": B, "expected_head": B}, (20, "auto_approve_reanchored")),
        ({"pinned_head": B, "expected_head": A}, (20, "stale_head")),
    ],
)
def test_the_unit_guard_table(override: dict[str, Any], want: tuple[int, str | None]) -> None:
    assert _reason(_check(**override)) == want


@pytest.mark.parametrize(
    "actor",
    [
        "auto:x",
        "auto:unknown",
        "AUTO:x",
        "robot",
        "",
        "humanx:a",
        "HUMAN:alice",
        " human:alice",
        "Human:a",
    ],
)
def test_every_actor_that_is_not_exactly_human_is_guarded(actor: str) -> None:
    assert _reason(_check(actor=actor, allow=False)) == (20, "auto_approve_disabled")


def test_guard_prepared_refuses_a_prepared_plan_even_if_the_order_missed_it(
    forgectl: FakeForgectl, tmp_path: Path
) -> None:
    """The write path re-checks the guard against the confirmed plan itself, so a later change to
    the order cannot let an automatic approval through."""
    prepared, _ = run(
        request("approve", expected_head=A, expected_base="main"), show_reply(), dry_reply()
    )
    config = LoadedConfig.defaults()
    assert approve_module.guard_prepared(prepared, config) is None  # a human
    auto_plan = dataclasses.replace(prepared.plan, actor=AUTO, decision_ids=())
    auto = dataclasses.replace(
        prepared, precheck=dataclasses.replace(prepared.precheck, plan=auto_plan)
    )
    refused = approve_module.guard_prepared(auto, config)
    assert refused is not None and _reason(refused) == (20, "auto_approve_disabled")


@pytest.mark.parametrize("command", ["approve", "review"])
def test_the_second_guard_alone_refuses_an_automatic_approval_before_any_write(
    command: str,
    cli: Run,
    forgectl: FakeForgectl,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_home: Path,
) -> None:
    """Mutant: ``cli.guard_prepared`` made a no-op. With the order's guard off, the opt-in off and
    ``--yes``, the run must still end at exit 20 with nothing written and no ``attempted``."""
    monkeypatch.setattr(prepare_module, "_guard", lambda *a, **k: None)
    cfg = write_config(tmp_path, "{}\n")
    if command == "approve":
        forgectl.script(show(), dry_approve())
        argv = auto_approve(cfg)
    else:
        forgectl.script(show(), dry_review())
        draft = draft_file(tmp_path, [finding("f1")])
        argv = review_argv(
            draft, "--config", str(cfg), "--expected-head", A, "--decision-id", GATE, actor=AUTO
        )
    code, error, _ = cli(*argv)
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    assert forgectl.writes() == []
    assert audit(state_home) == []
    assert [e["details"]["reason"] for e in log(state_home) if e["event"] == "refused"] == [
        "auto_approve_disabled"
    ]


@pytest.mark.parametrize("command", ["approve", "review"])
def test_both_guard_call_sites_pass_the_same_inputs(
    command: str,
    cli: Run,
    forgectl: FakeForgectl,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The order's guard and the guard on the confirmed plan must agree: same actor, decision ids,
    expected head, pinned head, anchor head and repo opt-in on one scenario."""
    seen: list[dict[str, Any]] = []
    real = approve_module.check_auto_approve

    def spy(**kwargs: Any) -> Outcome | None:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(approve_module, "check_auto_approve", spy)
    if command == "approve":
        forgectl.script(show(), dry_approve(), approved())
        argv = auto_approve(allowing(tmp_path))
    else:
        forgectl.script(show(), dry_review(), reviewed())
        draft = draft_file(tmp_path, [finding("f1")])
        argv = review_argv(
            draft,
            "--config",
            str(allowing(tmp_path)),
            "--expected-head",
            A,
            "--decision-id",
            GATE,
            actor=AUTO,
        )
    code, doc, _ = cli(*argv)
    assert code == 0, doc
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0]["actor"] == AUTO and seen[0]["allow"] is True
    assert seen[0]["decision_ids"] == (GATE,) or list(seen[0]["decision_ids"]) == [GATE]
    assert seen[0]["expected_head"] == A and seen[0]["pinned_head"] == A


# --- review --approve ------------------------------------------------------------------------


def review_argv(
    draft: str, *extra: str, actor: str = HUMAN, pr: str = PR, approve: bool = True
) -> tuple[str, ...]:
    flags = ("--approve",) if approve else ()
    return (
        "review",
        pr,
        "--draft-file",
        draft,
        "--json",
        "--actor",
        actor,
        "--yes",
        *flags,
        *extra,
    )


def test_a_human_review_approve_posts_the_findings_under_an_approving_review(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1"), finding("f2", "b.py", None, body="file")])
    forgectl.script(show(), dry_review(), reviewed())
    code, doc, _ = cli(*review_argv(draft))
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["action"] == "review" and doc["event"] == "approve"
    assert doc["dedupe_key"] == f"prc:ra:{doc['draft_id']}:{A[:12]}"
    assert doc["posted_inline"] == ["f1"] and doc["file_level"] == ["f2"]
    real = forgectl.writes()[0]
    assert real.subcommand == "review" and real.stdin is not None
    review = json.loads(real.stdin)
    assert review["event"] == "approve" and len(review["comments"]) == 1
    assert flag(real, "--expected-head") == [A] and flag(real, "--expected-base") == ["main"]
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["inputs"]["event"] == "approve" and records[1]["action"] == "review"
    assert records[1]["inputs"]["draft_id"] == doc["draft_id"]


def test_review_approve_with_a_request_changes_draft_is_exit_2_for_every_actor(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")], event="request_changes")
    for actor in (HUMAN, AUTO):
        code, error, _ = cli(
            *review_argv(draft, "--expected-head", A, "--decision-id", GATE, actor=actor)
        )
        assert code == 2 and error["error"]["code"] == "approve_conflicts_event"
    nothing_sent(forgectl, state_home, [])


def test_an_automatic_review_approve_posts_when_the_guard_is_satisfied(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry_review(), reviewed())
    code, doc, _ = cli(
        *review_argv(
            draft,
            "--config",
            str(allowing(tmp_path)),
            "--expected-head",
            A,
            "--decision-id",
            GATE,
            actor=AUTO,
        )
    )
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    real = forgectl.writes()[0]
    assert flag(real, "--actor") == [AUTO] and flag(real, "--decision-id") == [GATE]
    review = json.loads(real.stdin or b"")
    assert review["body"].startswith("Posted automatically under policy `policy`")
    assert audit(state_home)[0]["actor"] == AUTO


@pytest.mark.parametrize(
    ("case", "want_exit", "want_reason"),
    [
        ("disabled", 20, "auto_approve_disabled"),
        ("no decision id", 20, "auto_approve_disabled"),
        ("no expected head", 2, None),
        ("stale expected head", 20, "stale_head"),
    ],
)
def test_the_review_approve_guard_table(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    case: str,
    want_exit: int,
    want_reason: str | None,
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    config = write_config(tmp_path, "{}\n") if case == "disabled" else allowing(tmp_path)
    extra = ["--config", str(config)]
    if case != "no expected head":
        extra += ["--expected-head", B if case == "stale expected head" else A]
    if case != "no decision id":
        extra += ["--decision-id", GATE]
    if want_exit != 2:  # a usage error ends the run before show
        forgectl.script(show())
    code, error, _ = cli(*review_argv(draft, *extra, actor=AUTO))
    assert code == want_exit, (case, error)
    assert error["error"]["reason"] == want_reason
    # nothing past show: no compare, no pre-check, no write, no audit record
    assert subcommands(forgectl) == ([] if want_exit == 2 else ["show"])
    assert forgectl.writes() == [] and audit(state_home) == []


def test_an_automatic_review_approve_of_a_reanchored_draft_is_refused(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    """The gate and the draft saw A, not B: refused before any compare, pre-check or write."""
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(head=B))
    code, error, _ = cli(
        *review_argv(
            draft,
            "--config",
            str(allowing(tmp_path)),
            "--reanchor-to",
            B,
            "--expected-head",
            B,
            "--decision-id",
            GATE,
            actor=AUTO,
        )
    )
    assert code == 20 and error["error"]["reason"] == "auto_approve_reanchored"
    validate("pr-comment/cli/error.json", error)
    nothing_sent(forgectl, state_home, ["show"])


def test_a_reanchor_to_the_same_head_is_not_a_reanchor_for_the_guard(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry_review(), reviewed())
    code, doc, _ = cli(
        *review_argv(
            draft,
            "--config",
            str(allowing(tmp_path)),
            "--reanchor-to",
            A,
            "--expected-head",
            A,
            "--decision-id",
            GATE,
            actor=AUTO,
        )
    )
    assert code == 0, doc


def test_a_human_review_approve_may_reanchor_and_is_keyed_by_the_new_head(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(
        show(head=B),
        ok("compare", h.compare_doc(PR, from_sha=A, to_sha=B, diff="")),
        dry_review(head_sha=B),
        reviewed(head_sha=B),
    )
    code, doc, _ = cli(*review_argv(draft, "--reanchor-to", B))
    assert code == 0, doc
    assert doc["head_sha"] == B and doc["dedupe_key"] == f"prc:ra:{doc['draft_id']}:{B[:12]}"
    assert doc["reanchored_draft"]["anchor"]["head_sha"] == B


def test_a_review_approve_dry_run_validates_and_sends_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    golden_json: Callable[[str, Any], None],
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry_review())
    code, doc, _ = cli(*review_argv(draft, "--dry-run"))
    assert code == 0, doc
    validate("pr-comment/cli/review.json", doc)
    assert doc["dry_run"] is True and doc["event"] == "approve"
    assert forgectl.writes() == [] and audit(state_home) == []
    golden_json("review/approve-dry-run.json", _stable({**doc, "plan": None}))


def test_a_review_approve_dry_run_of_an_automatic_actor_applies_the_guard(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show())
    code, error, _ = cli(
        *review_argv(
            draft,
            "--dry-run",
            "--config",
            str(write_config(tmp_path, "{}\n")),
            "--expected-head",
            A,
            "--decision-id",
            GATE,
            actor=AUTO,
        )
    )
    assert code == 20 and error["error"]["reason"] == "auto_approve_disabled"
    nothing_sent(forgectl, state_home, ["show"])


def test_a_declined_review_approve_dialog_posts_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    interactive: Callable[[bool], list[Any]],
) -> None:
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry_review())
    shown = interactive(False)
    code, doc, _ = cli("review", PR, "--draft-file", draft, "--approve", "--json", "--actor", HUMAN)
    assert code == 13 and len(shown) == 1
    assert forgectl.writes() == [] and audit(state_home) == []


# --- audit failures --------------------------------------------------------------------------


def _fail_at(n: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from pr_comment import appendfile

    original = appendfile.AppendFile.append
    count = [0]

    def append(self: Any, line: bytes, *, sync: bool = False) -> None:
        if sync:
            count[0] += 1
            if count[0] >= n:
                raise appendfile.AppendError(
                    "no space left on device", "audit.jsonl", OSError(errno.ENOSPC, "full")
                )
        original(self, line, sync=sync)

    monkeypatch.setattr(appendfile.AppendFile, "append", append)


def test_a_failed_attempted_append_sends_no_approval(
    cli: Run, forgectl: FakeForgectl, state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgectl.script(show(), dry_approve())
    _fail_at(1, monkeypatch)
    code, doc, _ = cli(*human_approve())
    assert code == 12 and doc["error"]["code"] == "audit_unavailable"
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "same_command")
    assert forgectl.writes() == []


def test_a_failed_outcome_append_after_a_landed_approval_is_audit_failed_and_not_a_retry(
    cli: Run, forgectl: FakeForgectl, state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgectl.script(show(), dry_approve(), approved())
    _fail_at(2, monkeypatch)
    code, doc, _ = cli(*human_approve())
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "audit_failed" and (error["landed"], error["retry"]) == (True, "none")
    assert error["details"]["outcome"]["review_id"] == "gh:r:R_1"
    assert len(forgectl.writes()) == 1


def test_a_failed_outcome_append_after_a_refused_approval_keeps_the_writes_pair(
    cli: Run, forgectl: FakeForgectl, state_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forgectl.script(show(), dry_approve(), fail("approve", "outcome_unknown"))
    _fail_at(2, monkeypatch)
    code, doc, _ = cli(*human_approve())
    error = doc["error"]
    assert code == 12 and error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (None, "same_command")


# --- the refusal is logged -------------------------------------------------------------------


def test_a_guard_refusal_is_logged_as_refused_with_its_reason(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    forgectl.script(show())
    code, _, _ = cli(*auto_approve(write_config(tmp_path, "{}\n")))
    assert code == 20
    refused = [e for e in log(state_home) if e["event"] == "refused"]
    assert [e["details"]["reason"] for e in refused] == ["auto_approve_disabled"]
    assert refused[0]["level"] == "warn"


# --- signals ---------------------------------------------------------------------------------


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_during_the_approval_is_outcome_unknown_and_audited(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, signum: int
) -> None:
    forgectl.script(
        show(),
        dry_approve(),
        ok(
            "approve",
            h.post_doc("approve", ids=["gh:r:R_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    forgectl.allow_unused_steps = False
    n = forgectl.next_call_number() + 3  # show, the pre-check, --version, the write
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home)})
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pr_comment", *human_approve()],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,
    )
    forgectl.wait_ready(n)
    os.kill(proc.pid, signum)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    error = json.loads(stdout)["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert len(forgectl.writes()) == 1
