"""The default cleanup of the live suite (issue #92), offline: the host calls are a recording fake
(``gh`` and ``glab`` are small scripts in a temporary directory, or an injected ``call``). Nothing
here reaches a host."""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.live import cleanup
from tests.live.runner import Runner
from tests.live.settings import from_env

REPO = "github.com/acme/widgets"
PR = f"{REPO}#7"
LAB = "gitlab.com/acme/group/project"


def settings_for(tmp_path: Path, **extra: str) -> Any:
    return from_env(
        {
            "PR_COMMENT_LIVE": "1",
            "PR_COMMENT_LIVE_MODE": "run",
            "PR_COMMENT_LIVE_ALLOW": f"{REPO},{LAB}",
            "PR_COMMENT_LIVE_GITHUB_PR": PR,
            "PR_COMMENT_LIVE_REPORT_DIR": str(tmp_path / "report"),
            "PR_COMMENT_LIVE_RUN_ID": "cleanrun1",
            **extra,
        }
    )


class Recorder:
    """A stand-in for ``subprocess.run`` that records the argv and answers a fixed status."""

    def __init__(self, code: int = 0, stdout: bytes = b"{}") -> None:
        self.calls: list[list[str]] = []
        self.code = code
        self.stdout = stdout
        self.envs: list[dict[str, str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        self.envs.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, self.code, self.stdout, b"")


def bin_with(tmp_path: Path, *names: str) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name in names:
        script = directory / name
        script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return directory


def runner_with(
    tmp_path: Path,
    rows: list[tuple[str, str]],
    *,
    tools: tuple[str, ...] = ("gh", "glab"),
    **extra: str,
) -> tuple[Runner, Recorder]:
    settings = settings_for(tmp_path, **extra)
    runner = Runner(settings, tmp_path / "work", env={"PATH": str(bin_with(tmp_path, *tools))})
    recorder = Recorder()
    runner.cleanup_call = recorder
    runner.ledger = [{"run": "cleanrun1", "scenario": "s", "pr": pr, "id": i} for pr, i in rows]
    return runner, recorder


def test_it_deletes_issue_and_inline_comments_and_lists_what_a_host_cannot_delete(
    tmp_path: Path,
) -> None:
    runner, recorder = runner_with(
        tmp_path,
        [
            (PR, "gh:ic:IC_1"),
            (PR, "gh:rc:PRRC_2"),
            (PR, "gh:r:PRR_3"),
            (PR, "gh:rt:PRRT_4"),
        ],
    )
    notes = runner.teardown()
    names = [next(a for a in c if a.startswith("query=")) for c in recorder.calls]
    assert "deleteIssueComment" in names[0] and "deletePullRequestReviewComment" in names[1]
    assert len(recorder.calls) == 2
    assert [c[-1] for c in recorder.calls] == ["id=IC_1", "id=PRRC_2"]
    assert len(notes) == 2
    assert "gh:r:PRR_3" in notes[0] and "submitted review" in notes[0]
    assert "gh:rt:PRRT_4" in notes[1] and "thread" in notes[1]


def test_what_it_deleted_is_written_to_the_ledger_file(tmp_path: Path) -> None:
    runner, _ = runner_with(tmp_path, [(PR, "gh:ic:IC_1")])
    runner.teardown()
    rows = [
        json.loads(x)
        for x in (tmp_path / "report" / "ledger-cleanrun1.jsonl").read_text().splitlines()
    ]
    assert rows == [
        {"run": "cleanrun1", "scenario": "s", "pr": PR, "id": "gh:ic:IC_1", "status": "deleted"}
    ]


def test_a_repo_off_the_allowlist_is_never_touched(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [("github.com/acme/elsewhere#1", "gh:ic:IC_1")])
    notes = runner.teardown()
    assert recorder.calls == []
    assert len(notes) == 1 and "allowlist" in notes[0]


def test_keep_leaves_everything_and_says_so(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")], PR_COMMENT_LIVE_KEEP="1")
    notes = runner.teardown()
    assert recorder.calls == []
    assert len(notes) == 1 and "PR_COMMENT_LIVE_KEEP" in notes[0]


def test_a_cleanup_command_replaces_the_default(tmp_path: Path) -> None:
    command = tmp_path / "cleanup.sh"
    command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    command.chmod(0o700)
    runner, recorder = runner_with(
        tmp_path, [(PR, "gh:ic:IC_1")], PR_COMMENT_LIVE_CLEANUP_CMD=str(command)
    )
    assert runner.teardown() == []
    assert recorder.calls == []


def test_a_missing_gh_is_a_note_not_a_failure(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")], tools=())
    notes = runner.teardown()
    assert recorder.calls == []
    assert len(notes) == 1 and "gh was not found" in notes[0] and "CLEANUP_CMD" in notes[0]


@pytest.mark.parametrize("code,stdout", [(1, b""), (0, b'{"errors":[{"message":"nope"}]}')])
def test_a_failed_delete_is_a_note_with_the_status_and_never_the_output(
    tmp_path: Path, code: int, stdout: bytes
) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")])
    recorder.code, recorder.stdout = code, stdout
    notes = runner.teardown()
    assert len(notes) == 1 and "failed" in notes[0] and "nope" not in notes[0]
    ledger = tmp_path / "report" / "ledger-cleanrun1.jsonl"
    assert not ledger.exists() or "deleted" not in ledger.read_text()


def test_the_calls_run_in_the_private_child_environment(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")])
    runner.teardown()
    assert recorder.envs[0]["HOME"] == str(tmp_path / "work" / "home")
    assert not [k for k in recorder.envs[0] if k.startswith("PR_COMMENT_")]


def test_a_gitlab_note_is_deleted_through_the_rest_route_and_a_discussion_is_listed(
    tmp_path: Path,
) -> None:
    lab_pr = f"{LAB}#3"
    runner, recorder = runner_with(tmp_path, [(lab_pr, "gl:n:555"), (lab_pr, "gl:d:abcdef")])
    notes = runner.teardown()
    (call,) = recorder.calls
    assert call[1:] == [
        "api",
        "-X",
        "DELETE",
        "projects/acme%2Fgroup%2Fproject/merge_requests/3/notes/555",
    ]
    assert len(notes) == 1 and "gl:d:abcdef" in notes[0]


def test_an_enterprise_host_is_passed_to_the_cli() -> None:
    argv, why = cleanup.plan_delete("ghe.example.com/acme/widgets#5", "gh:ic:IC_1")
    assert why is None and argv is not None
    assert argv[:5] == ["gh", "api", "--hostname", "ghe.example.com", "graphql"]


@pytest.mark.parametrize("forge_id", ["", "gh:ic:", "gh:zz:abc", "nonsense", "gl:n:abc"])
def test_an_id_it_does_not_know_is_listed_not_guessed(forge_id: str) -> None:
    argv, why = cleanup.plan_delete(PR, forge_id)
    assert argv is None and why


def test_a_pr_reference_it_cannot_read_is_listed() -> None:
    argv, why = cleanup.plan_delete("not a pr", "gh:ic:IC_1")
    assert argv is None and why


def test_the_document_states_the_cleanup_expectation_and_what_cannot_be_deleted() -> None:
    from tests.live import checklist

    text = checklist.render()
    assert "PR_COMMENT_LIVE_KEEP" in text and "default cleanup" in text
    assert "submitted reviews and approvals" in text and "throwaway repo" in text


def test_the_line_separator_rows_are_recorded_as_observations_with_the_open_question() -> None:
    from tests.live import checklist

    row = next(r for r in checklist.ROWS if r.id == "ts.line_separators")
    assert "observe" in row.text and "line breaks" in row.text and "forgectl itself" in row.text
    assert checklist.automated("ts.line_separators")
