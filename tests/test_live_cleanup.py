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
GH_OK = b'{"data":{"deleteIssueComment":{"clientMutationId":null}}}'
GH_OK_RC = b'{"data":{"deletePullRequestReviewComment":{"clientMutationId":null}}}'


def settings_for(tmp_path: Path, **extra: str) -> Any:
    return from_env(
        {
            "PR_COMMENT_LIVE": "1",
            "PR_COMMENT_LIVE_MODE": "run",
            "PR_COMMENT_LIVE_ALLOW": f"{REPO},{LAB}",
            "PR_COMMENT_LIVE_GITHUB_PR": PR,
            "PR_COMMENT_LIVE_GITLAB_PR": f"{LAB}#3",
            "PR_COMMENT_LIVE_REPORT_DIR": str(tmp_path / "report"),
            "PR_COMMENT_LIVE_RUN_ID": "cleanrun1",
            **extra,
        }
    )


class Recorder:
    """A stand-in for ``subprocess.run`` that records the argv and answers a fixed status."""

    def __init__(self, code: int = 0, stdout: bytes | None = None) -> None:
        self.calls: list[list[str]] = []
        self.code = code
        self.stdout = stdout  # None: the success answer of whichever mutation was asked for
        self.envs: list[dict[str, str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        self.envs.append(kwargs["env"])
        out = self.stdout
        if out is None:
            asked = " ".join(argv)
            out = GH_OK_RC if "deletePullRequestReviewComment" in asked else GH_OK
            if argv[0].endswith("glab"):
                out = b""
        return subprocess.CompletedProcess(argv, self.code, out, b"")


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
    env: dict[str, str] | None = None,
    **extra: str,
) -> tuple[Runner, Recorder]:
    settings = settings_for(tmp_path, **extra)
    full_env = {"PATH": str(bin_with(tmp_path, *tools)), **(env or {})}
    runner = Runner(settings, tmp_path / "work", env=full_env)
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
        "--hostname",
        "gitlab.com",
        "-X",
        "DELETE",
        "projects/acme%2Fgroup%2Fproject/merge_requests/3/notes/555",
    ]
    assert len(notes) == 1 and "gl:d:abcdef" in notes[0]


def test_an_enterprise_host_is_passed_to_the_cli() -> None:
    argv, why = cleanup.plan_delete("ghe.example.com/acme/widgets#5", "gh:ic:IC_1")
    assert why is None and argv is not None
    assert argv[:5] == ["gh", "api", "--hostname", "ghe.example.com", "graphql"]


def test_the_host_is_lowercased_and_always_passed() -> None:
    argv, why = cleanup.plan_delete("GitHub.COM/acme/widgets#5", "gh:ic:IC_1")
    assert why is None and argv is not None
    assert argv[:5] == ["gh", "api", "--hostname", "github.com", "graphql"]


@pytest.mark.parametrize(
    "forge_id",
    [
        "",
        "gh:ic:",
        "gh:zz:abc",
        "nonsense",
        "gl:n:abc",
        "gl:n:5\u0663",
        "gh:ic:a b",
        "gh:ic:IC_1\nX",
        "gh:rc:a/b",
        "gh:ic:" + "A" * 101,
    ],
)
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


def test_the_line_separator_row_records_that_only_forgectl_breaks_lines() -> None:
    from tests.live import checklist

    row = next(r for r in checklist.ROWS if r.id == "ts.line_separators")
    assert "observe" in row.text and "does not break lines" in row.text
    assert "forgectl's own neutralizing step" in row.text and "still open" not in row.text
    assert "GitLab and enterprise hosts were not observed" in row.text
    assert checklist.automated("ts.line_separators")


# --- review round 1 of issue #92 ------------------------------------------------------------------


def _step(label: str = "l") -> Any:
    from tests.live.runner import Step

    return Step("s", label, "forgectl", [PR], True, applied="real")


def test_keep_wins_over_a_cleanup_command(tmp_path: Path) -> None:
    seen = tmp_path / "seen.txt"
    command = tmp_path / "cleanup.sh"
    command.write_text(f'#!/bin/sh\necho "$1 $2" >> {seen}\n', encoding="utf-8")
    command.chmod(0o700)
    runner, recorder = runner_with(
        tmp_path,
        [(PR, "gh:ic:IC_1")],
        PR_COMMENT_LIVE_KEEP="1",
        PR_COMMENT_LIVE_CLEANUP_CMD=str(command),
    )
    notes = runner.teardown()
    assert not seen.exists() and recorder.calls == []
    assert len(notes) == 1 and "PR_COMMENT_LIVE_KEEP" in notes[0]


@pytest.mark.parametrize("value", ["yes", "true", "2", "on"])
def test_a_keep_value_other_than_0_or_1_is_refused_by_name_only(value: str) -> None:
    settings = from_env({"PR_COMMENT_LIVE": "1", "PR_COMMENT_LIVE_KEEP": value})
    named = [p for p in settings.problems if "PR_COMMENT_LIVE_KEEP" in p]
    assert named and not any(value in p for p in named)
    assert settings.keep is False


@pytest.mark.parametrize(("value", "keep"), [("0", False), ("1", True)])
def test_a_keep_value_of_0_or_1_is_accepted(value: str, keep: bool) -> None:
    settings = from_env({"PR_COMMENT_LIVE": "1", "PR_COMMENT_LIVE_KEEP": value})
    assert not [p for p in settings.problems if "KEEP" in p]
    assert settings.keep is keep


FAKE_GH = """#!/bin/sh
if [ "$GH_TOKEN" = "sentinel-target-token" ] && [ -z "${GH_HOST+x}" ] && [ -z "${GH_REPO+x}" ]; then
  echo '{"data":{"deleteIssueComment":{"clientMutationId":null}}}'
else
  echo '{"data":null}'
fi
"""


def test_the_cleanup_call_carries_the_targets_token_and_not_gh_host_or_gh_repo(
    tmp_path: Path,
) -> None:
    runner, _ = runner_with(
        tmp_path,
        [(PR, "gh:ic:IC_1")],
        env={
            "PRCLIVE_TOKEN_GITHUB": "sentinel-target-token",
            "GH_TOKEN": "sentinel-ambient-token",
            "GH_HOST": "elsewhere.invalid",
            "GH_REPO": "other/repo",
        },
    )
    gh = Path(runner.env["PATH"]) / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    runner.cleanup_call = subprocess.run
    assert runner.teardown() == []
    ledger = (tmp_path / "report" / "ledger-cleanrun1.jsonl").read_text()
    assert '"status": "deleted"' in ledger


def test_the_token_is_chosen_by_the_row_target_and_kind(tmp_path: Path) -> None:
    lab_pr = f"{LAB}#3"
    runner, recorder = runner_with(
        tmp_path,
        [(PR, "gh:ic:IC_1"), (lab_pr, "gl:n:555")],
        env={
            "PRCLIVE_TOKEN_GITHUB": "sentinel-gh",
            "PRCLIVE_TOKEN_GITLAB": "sentinel-gl",
            "GH_TOKEN": "sentinel-ambient",
        },
    )
    runner.teardown()
    gh_env, gl_env = recorder.envs
    gh_ok = gh_env.get("GH_TOKEN") == "sentinel-gh" and "GITLAB_TOKEN" not in gh_env
    gl_ok = (
        gl_env.get("GITLAB_TOKEN") == "sentinel-gl" and gl_env.get("GH_TOKEN") == "sentinel-ambient"
    )
    assert gh_ok
    assert gl_ok


def test_without_a_token_variable_the_environment_is_left_as_it_is(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")], env={"GH_TOKEN": "sentinel-a"})
    runner.teardown()
    kept = recorder.envs[0].get("GH_TOKEN") == "sentinel-a"
    assert kept


def test_the_cleanup_call_env_drops_gh_host_and_gh_repo(tmp_path: Path) -> None:
    runner, recorder = runner_with(
        tmp_path, [(PR, "gh:ic:IC_1")], env={"GH_HOST": "x.invalid", "GH_REPO": "o/r"}
    )
    runner.teardown()
    assert "GH_HOST" not in recorder.envs[0] and "GH_REPO" not in recorder.envs[0]


def test_a_repeated_id_is_ledgered_deleted_and_noted_once(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [])
    runner.verified.add(PR)
    runner._ledger_ids(_step("a"), ["gh:ic:IC_1"])
    runner._ledger_ids(_step("b"), ["gh:ic:IC_1", "gh:ic:IC_2"])
    assert [r["id"] for r in runner.ledger] == ["gh:ic:IC_1", "gh:ic:IC_2"]
    assert runner.teardown() == []
    assert len(recorder.calls) == 2
    rows = (tmp_path / "report" / "ledger-cleanrun1.jsonl").read_text().splitlines()
    assert len(rows) == 4  # two posted rows, two deleted rows


@pytest.mark.parametrize(
    "stdout",
    [
        b"not json",
        b"",
        b"[]",
        b'{"data":null}',
        b'{"data":{"deleteIssueComment":null}}',
        b'{"data":{"deletePullRequestReviewComment":{}}}',
        b'{"data":{"deleteIssueComment":{}},"errors":[{"message":"nope"}]}',
    ],
)
def test_a_github_delete_counts_only_when_the_mutation_answered_with_an_object(
    tmp_path: Path, stdout: bytes
) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")])
    recorder.stdout = stdout
    notes = runner.teardown()
    assert len(notes) == 1 and "left on" in notes[0] and "nope" not in notes[0]
    ledger = tmp_path / "report" / "ledger-cleanrun1.jsonl"
    assert not ledger.exists() or "deleted" not in ledger.read_text()


def test_a_github_delete_with_an_empty_error_list_and_an_object_counts(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [(PR, "gh:ic:IC_1")])
    recorder.stdout = b'{"errors":[],"data":{"deleteIssueComment":{}}}'
    assert runner.teardown() == []


def test_a_gitlab_delete_is_judged_by_the_exit_code_alone(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [(f"{LAB}#3", "gl:n:555")])
    recorder.stdout = b""
    assert runner.teardown() == []


def test_ids_read_from_unparsable_output_are_listed_and_never_deleted(tmp_path: Path) -> None:
    from tests.live.runner import LiveFailure

    runner, recorder = runner_with(tmp_path, [])
    runner.verified.add(PR)
    with pytest.raises(LiveFailure):
        runner._outcome(_step("w"), 1, b"oops gh:ic:IC_9 oops", b"")
    notes = runner.teardown()
    assert recorder.calls == []
    assert len(notes) == 1 and "gh:ic:IC_9" in notes[0] and "left on" in notes[0]


def test_ids_of_an_already_posted_answer_are_listed_and_never_deleted(tmp_path: Path) -> None:
    runner, recorder = runner_with(tmp_path, [])
    runner.verified.add(PR)
    runner._settle(_step("w"), 0, {"ids": ["gh:ic:IC_8"], "already_posted": True})
    notes = runner.teardown()
    assert recorder.calls == []
    assert len(notes) == 1 and "gh:ic:IC_8" in notes[0] and "left on" in notes[0]


def test_an_id_this_run_posted_stays_deletable_when_a_repeat_says_already_posted(
    tmp_path: Path,
) -> None:
    runner, recorder = runner_with(tmp_path, [])
    runner.verified.add(PR)
    runner._settle(_step("a"), 0, {"ids": ["gh:ic:IC_1"], "already_posted": False})
    runner._settle(_step("b"), 0, {"ids": ["gh:ic:IC_1"], "already_posted": True})
    assert runner.teardown() == []
    assert len(recorder.calls) == 1


def test_notes_show_ids_and_refs_with_repr_so_no_raw_newline_gets_through(tmp_path: Path) -> None:
    runner, _ = runner_with(
        tmp_path,
        [(PR, "gh:ic:IC_1\nforged line"), ("github.com/acme/elsewhere#1\nx", "gh:ic:IC_2")],
    )
    notes = runner.teardown()
    assert len(notes) == 2
    assert all("\n" not in n and "\\n" in n for n in notes)


def test_the_document_says_what_is_left_on_gitlab_and_that_the_route_is_unverified() -> None:
    from tests.live import checklist

    text = checklist.render()
    assert "discussion" in text and "`gl:n:`" in text and "not verified live" in text
    assert "`PRCLIVE_TOKEN_<KEY>` of the target" in text


GHE_REPO = "ghe.example.test/acme/widgets"
GHE_FAKE_GH = """#!/bin/sh
if [ "$GH_ENTERPRISE_TOKEN" = "sentinel-ghe" ] && [ "$GH_TOKEN" = "sentinel-ghe" ]; then
  echo '{"data":{"deleteIssueComment":{"clientMutationId":null}}}'
else
  echo '{"data":null}'
fi
"""


def test_a_gh_call_to_an_enterprise_host_also_gets_gh_enterprise_token(tmp_path: Path) -> None:
    ghe_pr = f"{GHE_REPO}#5"
    runner, recorder = runner_with(
        tmp_path,
        [(ghe_pr, "gh:ic:IC_1"), (PR, "gh:ic:IC_2")],
        env={
            "PRCLIVE_TOKEN_GITHUB_ENTERPRISE": "sentinel-ghe",
            "PRCLIVE_TOKEN_GITHUB": "sentinel-gh",
        },
        PR_COMMENT_LIVE_ALLOW=f"{REPO},{LAB},{GHE_REPO}",
        PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR=ghe_pr,
    )
    runner.teardown()
    ghe_env, github_env = recorder.envs
    assert ghe_env.get("GH_ENTERPRISE_TOKEN") == "sentinel-ghe"
    assert "GH_ENTERPRISE_TOKEN" not in github_env


def test_the_enterprise_token_authenticates_a_fake_gh_end_to_end(tmp_path: Path) -> None:
    ghe_pr = f"{GHE_REPO}#5"
    runner, _ = runner_with(
        tmp_path,
        [(ghe_pr, "gh:ic:IC_1")],
        env={"PRCLIVE_TOKEN_GITHUB_ENTERPRISE": "sentinel-ghe"},
        PR_COMMENT_LIVE_ALLOW=f"{REPO},{LAB},{GHE_REPO}",
        PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR=ghe_pr,
    )
    gh = Path(runner.env["PATH"]) / "gh"
    gh.write_text(GHE_FAKE_GH, encoding="utf-8")
    runner.cleanup_call = subprocess.run
    assert runner.teardown() == []
