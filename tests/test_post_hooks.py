# ruff: noqa: F811
# (the imported fixtures ``cli`` and ``state_home`` are then used as test arguments)
"""The ``pre-post`` and ``post-post`` hooks around a real write (issue #33), through the real CLI
against the fake forgectl process, with real tiny hook scripts.

Hooks are shell scripts under ``tmp_path`` that set ``PATH=/usr/bin:/bin`` themselves (the test's
PATH holds only the fake forgectl). Every test runs under a private HOME and state directory.
Each hook records its PID in ``pids``; whatever is still alive at teardown is killed by exact PID.
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
from tests.forgectl_harness import FakeForgectl, fail, ok
from tests.posting import PR, A, finding
from tests.schema_harness import validate
from tests.test_hooks import alive, pids, signal_self_when_started, wait_dead, wait_file  # noqa: F401  (pids: fixture)
from tests.test_review_post import (  # noqa: F401  (cli and state_home are fixtures)
    ACTOR,
    Run,
    args_for,
    audit,
    calls_of,
    cli,
    draft_file,
    dry,
    flag,
    log,
    posted,
    reviews,
    show,
    state_home,
)


class Hooks:
    """Builds hook scripts that log their runs to files under ``root``."""

    def __init__(self, root: Path, pids: Path, state_home: Path) -> None:
        self.root = root
        self.pids = pids
        self.state_home = state_home
        self.count = 0

    def script(self, body: str) -> str:
        self.count += 1
        path = self.root / f"hook{self.count}.sh"
        path.write_text(f"#!/bin/sh\nPATH=/usr/bin:/bin\necho $$ >> {self.pids}\n{body}\n")
        path.chmod(0o700)
        return str(path)

    def recorder(self, name: str, *, exit_code: int = 0, extra: str = "") -> str:
        """A hook that saves its stdin to ``<name>.json``, appends ``name`` to ``order`` and the
        number of audit lines it can see to ``<name>.audit``, then runs ``extra`` and exits."""
        audit_file = self.state_home / "pr-comment" / "audit.jsonl"
        body = (
            f"cat > {self.root}/{name}.json\n"
            f"echo {name} >> {self.root}/order\n"
            f"(wc -l < {audit_file} 2>/dev/null || echo 0) > {self.root}/{name}.audit\n"
            f"env > {self.root}/{name}.env\n"
            f"pwd -P > {self.root}/{name}.cwd\n"
            f"{extra}\nexit {exit_code}"
        )
        return self.script(body)

    def payload(self, name: str) -> dict[str, Any]:
        document: dict[str, Any] = json.loads((self.root / f"{name}.json").read_text())
        return document

    def order(self) -> list[str]:
        path = self.root / "order"
        return path.read_text().split() if path.exists() else []

    def ran(self, name: str) -> bool:
        return (self.root / f"{name}.json").exists()


@pytest.fixture
def hooks(tmp_path: Path, pids: Path, state_home: Path) -> Hooks:
    return Hooks(tmp_path, pids, state_home)


def review_run(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, *extra: str, blocked: bool = False
) -> tuple[int, Any, str]:
    """A review run; ``blocked``: no write is expected (veto or escalation), none is scripted."""
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), *(() if blocked else (posted(),)))
    return cli(*args_for(draft, *extra))


def blocked_run(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, *extra: str
) -> tuple[int, Any, str]:
    return review_run(cli, forgectl, tmp_path, *extra, blocked=True)


# --- pre-post ----------------------------------------------------------------------------------


def test_pre_post_runs_before_attempted_and_the_write_with_a_valid_payload(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    pre = hooks.recorder("pre")
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--hook", f"pre-post={pre}")
    assert code == 0, doc
    payload = hooks.payload("pre")
    validate("pr-comment/hooks/pre-post.json", payload)
    assert payload["hook"] == "pre-post" and payload["action"] == "review"
    assert payload["write_index"] == 0 and payload["object_id"] == PR
    assert payload["plan_sha256"] == doc["plan_sha256"] and payload["event"] == "comment"
    assert payload["anchor_head"] is not None and payload["to"] is None
    assert payload["decision_ids"] == [] and payload["actor"] == ACTOR
    assert payload["thread_id"] is None and payload["thread_line"] is None
    assert [b["role"] for b in payload["bodies"]] == ["body", "inline:f1"]
    assert all("text" not in b for b in payload["bodies"])
    # before the attempted record: no audit line yet
    assert (tmp_path / "pre.audit").read_text().strip() == "0"
    records = audit(state_home)
    assert payload["attempt_id"] == records[0]["attempt_id"]


def test_hooks_cannot_change_what_is_posted(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    # a hook that scribbles on every file it can reach must not alter the confirmed plan
    pre = hooks.recorder("pre", extra=f"echo changed > {tmp_path}/draft.json")
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--hook", f"pre-post={pre}")
    assert code == 0, doc
    calls = reviews(forgectl)
    assert calls[-1].stdin == calls[-2].stdin  # the real write carries the pre-checked bytes


def test_config_hooks_run_before_hook_flags_in_order(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    first, second, third = (hooks.recorder(n) for n in ("c1", "c2", "f1"))
    config = tmp_path / "config.yaml"
    config.write_text(
        f"hooks:\n  pre-post:\n    - cmd: {first}\n    - cmd: {second}\n",
        encoding="utf-8",
    )
    code, doc, _ = review_run(
        cli, forgectl, tmp_path, "--config", str(config), "--hook", f"pre-post={third}"
    )
    assert code == 0, doc
    assert hooks.order() == ["c1", "c2", "f1"]


def test_a_veto_sends_nothing_and_writes_no_audit_record(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    veto = hooks.recorder("veto", exit_code=3, extra="echo 'no way' >&2")
    later = hooks.recorder("later")
    code, doc, _ = blocked_run(
        cli, forgectl, tmp_path, "--hook", f"pre-post={veto}", "--hook", f"pre-post={later}"
    )
    assert code == 11
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "vetoed" and error["hook"] == "pre-post"
    assert error["hook_stderr"] == "no way\n" and error["landed"] is None
    assert "no way" in error["message"]
    assert not hooks.ran("later")
    assert forgectl.writes() == []
    assert audit(state_home) == []
    assert [e["event"] for e in log(state_home) if e["event"] == "vetoed"] == ["vetoed"]


def test_the_veto_stderr_is_cut_to_4096_bytes(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    veto = hooks.script("head -c 100000 /dev/zero | tr '\\0' 'e' >&2; exit 2")
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, "--hook", f"pre-post={veto}")
    assert code == 11
    assert len(doc["error"]["hook_stderr"].encode()) == 4096
    validate("pr-comment/cli/error.json", doc)


def test_a_pre_post_timeout_is_a_veto_and_the_group_is_gone(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, pids: Path
) -> None:
    config = tmp_path / "config.yaml"
    slow = hooks.script("sleep 60 & echo $! >> " + str(pids) + "; wait")
    config.write_text(
        f"hooks:\n  pre-post:\n    - cmd: {slow}\n      timeout: 300ms\n", encoding="utf-8"
    )
    started = time.monotonic()
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, "--config", str(config))
    assert code == 11 and doc["error"]["code"] == "vetoed"
    assert time.monotonic() - started < 30
    assert forgectl.writes() == []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(alive(int(p)) for p in pids.read_text().split()):
        time.sleep(0.05)
    assert not any(alive(int(p)) for p in pids.read_text().split())


def test_a_hook_killed_by_a_signal_vetoes(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    dead = hooks.script("kill -9 $$")
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, "--hook", f"pre-post={dead}")
    assert code == 11 and forgectl.writes() == []


def test_a_hook_that_cannot_run_vetoes(cli: Run, forgectl: FakeForgectl, tmp_path: Path) -> None:
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, "--hook", "pre-post=/nonexistent/path/hook")
    assert code == 11 and forgectl.writes() == []


def test_an_escalation_runs_the_rest_then_exits_13_with_the_first_stderr(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13, extra="echo first >&2")
    ok1 = hooks.recorder("ok1")
    e2 = hooks.recorder("e2", exit_code=13, extra="echo second >&2")
    code, doc, _ = blocked_run(
        cli,
        forgectl,
        tmp_path,
        *(a for h in (e1, ok1, e2) for a in ("--hook", f"pre-post={h}")),
    )
    assert code == 13
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "escalated" and error["escalated_by"] == "pr-comment"
    assert error["escalated_hooks"] == [
        {"hook": "pre-post", "index": 0},
        {"hook": "pre-post", "index": 2},
    ]
    assert error["hook"] == "pre-post" and error["hook_stderr"] == "first\n"
    assert error["message"].endswith(": first") and "second" not in error["message"]
    assert hooks.order() == ["e1", "ok1", "e2"]
    assert forgectl.writes() == [] and audit(state_home) == []
    assert [e["event"] for e in log(state_home) if e["event"] == "escalated"] == ["escalated"]


def test_a_later_veto_beats_an_earlier_escalation(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    v = hooks.recorder("v", exit_code=1)
    code, doc, _ = blocked_run(
        cli, forgectl, tmp_path, "--hook", f"pre-post={e1}", "--hook", f"pre-post={v}"
    )
    assert code == 11 and doc["error"]["code"] == "vetoed"


def test_override_pr_comment_counts_an_escalation_as_allow_but_not_a_veto(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = review_run(
        cli,
        forgectl,
        tmp_path,
        "--hook",
        f"pre-post={e1}",
        "--override-escalation=pr-comment",
    )
    assert code == 0, doc
    assert len(forgectl.writes()) == 1
    assert "--override-escalation" not in reviews(forgectl)[-1].argv
    payload = hooks.payload("e1")
    assert payload["override_escalation"] == ["pr-comment"]
    assert "override_escalation" in audit(state_home)[0]["override"]


def test_override_forgectl_does_not_allow_a_pr_comment_escalation(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    e1 = hooks.recorder("e1", exit_code=13)
    code, doc, _ = blocked_run(
        cli, forgectl, tmp_path, "--hook", f"pre-post={e1}", "--override-escalation=forgectl"
    )
    assert code == 13 and forgectl.writes() == []


def test_override_pr_comment_passes_no_flag_to_forgectl_but_forgectl_scope_does(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    ok_hook = hooks.recorder("o")
    code, doc, _ = review_run(
        cli, forgectl, tmp_path, "--hook", f"pre-post={ok_hook}", "--override-escalation=forgectl"
    )
    assert code == 0, doc
    assert "--override-escalation" in reviews(forgectl)[-1].argv
    assert hooks.payload("o")["override_escalation"] == ["forgectl"]


def test_a_dry_run_and_a_refused_precheck_run_no_hook(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    h1 = hooks.recorder("h1")
    code, doc, _ = blocked_run(
        cli,
        forgectl,
        tmp_path,
        "--hook",
        f"pre-post={h1}",
        "--hook",
        f"post-post={h1}",
        "--dry-run",
    )
    assert code == 0 and not hooks.ran("h1")


def test_a_declined_dialog_runs_no_hook(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    h1 = hooks.recorder("h1")
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry())
    code, doc, _ = cli(
        "review", PR, "--draft-file", draft, "--json", "--actor", ACTOR, "--hook", f"pre-post={h1}"
    )
    assert code == 13 and not hooks.ran("h1")  # confirmation required: no TTY and no --yes


# --- decision ids ------------------------------------------------------------------------------


def test_a_hook_id_goes_to_forgectl_and_the_outcome_but_not_the_attempted_record(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    gate = hooks.script('echo \'{"decision_id": ["gate:1", "bad id"]}\'')
    seen = hooks.recorder("seen")
    code, doc, _ = review_run(
        cli,
        forgectl,
        tmp_path,
        "--decision-id",
        "caller-1",
        "--hook",
        f"pre-post={gate}",
        "--hook",
        f"pre-post={seen}",
    )
    assert code == 0, doc
    assert flag(reviews(forgectl)[-1], "--decision-id") == ["caller-1", "gate:1"]
    assert hooks.payload("seen")["decision_ids"] == ["caller-1", "gate:1"]
    attempted, outcome = audit(state_home)
    assert attempted["decision_id"] == ["caller-1"] and outcome["decision_id"] == ["caller-1"]
    assert outcome["hook_decision_id"] == ["gate:1"]
    assert "hook_decision_id" not in attempted
    assert any(e["event"] == "warning" for e in log(state_home))


def test_a_hook_id_never_satisfies_the_approval_guard(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    gate = hooks.script('echo \'{"decision_id": "gate:allow"}\'')
    forgectl.script(show())
    code, doc, _ = cli(
        "approve", PR, "--expected-head", A, "--expected-base", "main", "--json",
        "--actor", "auto:bot", "--yes", "--hook", f"pre-post={gate}",
    )  # fmt: skip
    assert code == 20 and doc["error"]["reason"] == "auto_approve_disabled"
    assert forgectl.writes() == [] and not (tmp_path / "pids").read_text().strip()


def test_a_hook_id_never_stands_in_for_the_callers_decision_id(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    # automatic approval is allowed here, so the refusal can only be the missing decision id
    config = tmp_path / "config.yaml"
    config.write_text("allow_auto_approve: true\n", encoding="utf-8")
    config.chmod(0o600)
    gate = hooks.script('echo \'{"decision_id": "gate:allow"}\'')
    forgectl.script(show())
    code, doc, _ = cli(
        "approve", PR, "--expected-head", A, "--expected-base", "main", "--json",
        "--actor", "auto:bot", "--yes", "--config", str(config), "--hook", f"pre-post={gate}",
    )  # fmt: skip
    assert code == 20 and doc["error"]["reason"] == "auto_approve_disabled"
    assert "--decision-id" in doc["error"]["message"]
    assert forgectl.writes() == [] and not (tmp_path / "pids").read_text().strip()


def test_an_escalating_hooks_id_is_neither_forwarded_nor_recorded(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    esc = hooks.script('echo \'{"decision_id": "esc:1"}\'; exit 13')
    good = hooks.script('echo \'{"decision_id": "good:1"}\'')
    code, doc, _ = review_run(
        cli,
        forgectl,
        tmp_path,
        "--override-escalation=pr-comment",
        "--hook", f"pre-post={esc}",
        "--hook", f"pre-post={good}",
    )  # fmt: skip
    assert code == 0, doc
    assert flag(reviews(forgectl)[-1], "--decision-id") == ["good:1"]
    assert audit(state_home)[1]["hook_decision_id"] == ["good:1"]


def test_a_veto_message_carries_only_the_first_stderr_line_sanitized(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    veto = hooks.script("printf 'first \\033[31mred\\033[0m\\007 end\\nsecond line\\n' >&2; exit 3")
    code, doc, _ = blocked_run(cli, forgectl, tmp_path, "--hook", f"pre-post={veto}")
    assert code == 11
    message = doc["error"]["message"]
    assert "first" in message and "second" not in message
    assert not any(ord(c) < 0x20 or ord(c) == 0x7F for c in message)
    assert "<U+001B>" in message
    assert "second line" in doc["error"]["hook_stderr"]  # the field itself is the full stderr


def test_decision_ids_are_capped_at_32_in_all(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    many = ", ".join(f'\\"h{i}\\"' for i in range(40))
    gate = hooks.script(f'echo "{{\\"decision_id\\": [{many}]}}"')
    code, doc, _ = review_run(
        cli, forgectl, tmp_path, "--decision-id", "c1", "--hook", f"pre-post={gate}"
    )
    assert code == 0, doc
    ids = flag(reviews(forgectl)[-1], "--decision-id")
    assert len(ids) == 32 and ids[0] == "c1" and ids[1] == "h0"
    assert audit(state_home)[1]["hook_decision_id"] == [f"h{i}" for i in range(31)]


def test_garbage_stdout_is_a_warning_and_the_write_goes_on(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    junk = hooks.script("printf '\\377\\376 not json'")
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--hook", f"pre-post={junk}")
    assert code == 0, doc
    assert audit(state_home)[1]["hook_decision_id"] == []
    assert any(
        e["event"] == "warning" and e["details"]["reason"] == "hook_stdout_invalid"
        for e in log(state_home)
    )


# --- environment, directory, text --------------------------------------------------------------


def test_the_hook_environment_directory_and_with_text(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    hooks: Hooks,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GH_TOKEN", "tok-secret-1")
    monkeypatch.setenv("GITLAB_TOKEN", "tok-secret-2")
    monkeypatch.setenv("DENIED_VAR", "deny-me")
    config = tmp_path / "config.yaml"
    pre = hooks.recorder("pre")
    config.write_text(
        "hook_env_deny: [DENIED_VAR]\nhooks:\n  pre-post:\n"
        f"    - cmd: {pre}\n      with_text: true\n",
        encoding="utf-8",
    )
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--config", str(config))
    assert code == 0, doc
    env_text = (tmp_path / "pre.env").read_text()
    assert "tok-secret" not in env_text and "deny-me" not in env_text
    assert "PR_COMMENT_HOOK=pre-post" in env_text and f"PR_COMMENT_ID={PR}" in env_text
    assert "PR_COMMENT_OBJECT_TYPE=pr" in env_text
    cwd = Path((tmp_path / "pre.cwd").read_text().strip())
    assert cwd.parent == (state_home / "pr-comment" / "tmp").resolve()
    assert not cwd.exists()  # removed after the hook
    payload = hooks.payload("pre")
    validate("pr-comment/hooks/pre-post.json", payload)
    assert all(isinstance(b["text"], str) and b["text"] for b in payload["bodies"])
    # the text is stdin only: never argv or env
    for b in payload["bodies"]:
        assert b["text"] not in env_text


def test_the_state_tmp_directory_is_empty_after_the_run(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    h1 = hooks.recorder("h1", extra="touch scratch")
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--hook", f"pre-post={h1}")
    assert code == 0, doc
    assert list((state_home / "pr-comment" / "tmp").iterdir()) == []


# --- post-post ---------------------------------------------------------------------------------


def test_post_post_runs_after_the_write_with_the_result(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    post = hooks.recorder("post")
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--hook", f"post-post={post}")
    assert code == 0, doc
    payload = hooks.payload("post")
    validate("pr-comment/hooks/post-post.json", payload)
    result = payload["result"]
    assert result["exit"] == 0 and result["code"] is None and result["reason"] is None
    assert result["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"] and result["review_id"] == "gh:r:R_1"
    assert result["already_posted"] is False and result["changed"] is None
    # after attempted, before the outcome record: one audit line
    assert (tmp_path / "post.audit").read_text().strip() == "1"
    assert len(audit(state_home)) == 2


def test_post_post_runs_when_the_write_failed_and_cannot_change_the_outcome(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    post = hooks.recorder("post", exit_code=7)
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(), fail("review", "rate_limited", phase="write", retry_after=5))
    code, doc, _ = cli(*args_for(draft, "--hook", f"post-post={post}"))
    assert code == 12 and doc["error"]["code"] == "rate_limited"
    payload = hooks.payload("post")
    validate("pr-comment/hooks/post-post.json", payload)
    assert payload["result"]["exit"] == 12 and payload["result"]["code"] == "rate_limited"
    assert payload["result"]["ids"] == [] and payload["result"]["already_posted"] is None
    assert audit(state_home)[1]["result"] == "failed"


@pytest.mark.parametrize("body", ["exit 9", "kill -9 $$", "sleep 60", "head -c 3000000 /dev/zero"])
def test_a_misbehaving_post_post_hook_never_changes_a_landed_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path, body: str
) -> None:
    config = tmp_path / "config.yaml"
    bad = hooks.script(body)
    config.write_text(
        f"hooks:\n  post-post:\n    - cmd: {bad}\n      timeout: 400ms\n", encoding="utf-8"
    )
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--config", str(config))
    assert code == 0, doc
    assert doc["ids"] == ["gh:rc:RC_1", "gh:rc:RC_2"] and doc["warnings"] == []
    records = audit(state_home)
    assert records[1]["result"] == "done" and len(forgectl.writes()) == 1
    assert any(
        e["event"] == "warning" and e["details"]["hook"] == "post-post" for e in log(state_home)
    )


def test_post_post_ids_are_recorded_after_the_pre_post_ones(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    pre = hooks.script('echo \'{"decision_id": "pre:1"}\'')
    post1 = hooks.script('echo \'{"decision_id": "post:1"}\'')
    post2 = hooks.recorder("post2")
    code, doc, _ = review_run(
        cli,
        forgectl,
        tmp_path,
        "--hook", f"pre-post={pre}",
        "--hook", f"post-post={post1}",
        "--hook", f"post-post={post2}",
    )  # fmt: skip
    assert code == 0, doc
    assert audit(state_home)[1]["hook_decision_id"] == ["pre:1", "post:1"]
    assert hooks.payload("post2")["decision_ids"] == ["pre:1", "post:1"]
    assert flag(reviews(forgectl)[-1], "--decision-id") == ["pre:1"]  # post ids come too late


def test_post_post_runs_for_an_already_posted_retry(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    pre = hooks.recorder("pre")
    post = hooks.recorder("post")
    draft = draft_file(tmp_path, [finding("f1")])
    forgectl.script(show(), dry(already_posted=True), posted(already_posted=True))
    code, doc, _ = cli(*args_for(draft, "--hook", f"pre-post={pre}", "--hook", f"post-post={post}"))
    assert code == 0 and doc["already_posted"] is True
    assert hooks.payload("post")["result"]["already_posted"] is True and hooks.ran("pre")


def test_an_async_post_post_hook_is_not_waited_for(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    config = tmp_path / "config.yaml"
    done = tmp_path / "async.done"
    slow = hooks.script(f"cat > {tmp_path}/async.json; sleep 1; echo ok > {done}")
    config.write_text(
        f"hooks:\n  post-post:\n    - cmd: {slow}\n      async: true\n", encoding="utf-8"
    )
    started = time.monotonic()
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--config", str(config))
    assert code == 0, doc
    assert time.monotonic() - started < 3
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not done.exists():
        time.sleep(0.05)
    assert done.exists()
    validate("pr-comment/hooks/post-post.json", json.loads((tmp_path / "async.json").read_text()))


def test_an_async_hook_over_its_timeout_changes_neither_the_exit_code_nor_the_audit(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    config = tmp_path / "config.yaml"
    runaway = hooks.script('echo \'{"decision_id": "late-id"}\'; sleep 60')
    config.write_text(
        f"hooks:\n  post-post:\n    - cmd: {runaway}\n      async: true\n      timeout: 1s\n",
        encoding="utf-8",
    )
    code, doc, _ = review_run(cli, forgectl, tmp_path, "--config", str(config))
    assert code == 0, doc
    assert wait_file(hooks.pids, 10)  # the hook has started (the run did not wait for it)
    hook_pid = int(hooks.pids.read_text().split()[0])
    assert wait_dead(hook_pid, 15)  # ended by its watchdog, long before its 60 s
    deadline = time.monotonic() + 5
    tmp_dir = state_home / "pr-comment" / "tmp"
    while time.monotonic() < deadline and (
        not any(r["event"] == "warning" for r in log(state_home)) or os.listdir(tmp_dir)
    ):
        time.sleep(0.05)  # the watchdog logs, then removes the directory
    warnings = [r for r in log(state_home) if r["event"] == "warning"]
    assert [r["details"]["reason"] for r in warnings] == ["hook_timeout"]
    assert warnings[0]["details"]["hook"] == "post-post" and "late-id" not in json.dumps(warnings)
    assert "late-id" not in json.dumps(audit(state_home))  # no ids from an async hook
    assert os.listdir(tmp_dir) == []


# --- comment: the null fields of the payload ---------------------------------------------------


def test_a_comment_payload_has_null_event_anchor_and_thread_fields(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path
) -> None:
    from tests import forgectl_harness as h

    body = tmp_path / "body.txt"
    body.write_text("looks good", encoding="utf-8")
    pre = hooks.recorder("pre")
    post = hooks.recorder("post")
    forgectl.script(
        show(),
        ok("comment", h.post_doc("comment", dry_run=True)),
        ok("comment", h.post_doc("comment", ids=["gh:ic:IC_1"])),
    )
    code, doc, _ = cli(
        "comment", PR, "--body-file", str(body), "--json", "--actor", ACTOR, "--yes",
        "--hook", f"pre-post={pre}", "--hook", f"post-post={post}",
    )  # fmt: skip
    assert code == 0, doc
    for name in ("pre", "post"):
        payload = hooks.payload(name)
        validate(f"pr-comment/hooks/{'pre' if name == 'pre' else 'post'}-post.json", payload)
        assert payload["action"] == "comment" and payload["event"] is None
        assert payload["anchor_head"] is None and payload["to"] is None
        assert payload["bodies"][0]["role"] == "body"
    assert [c.subcommand for c in calls_of(forgectl)] == ["show", "comment", "comment"]


# --- reply and approve: the null fields of the payload ----------------------------------------


def with_text_config(tmp_path: Path, pre: str, post: str) -> Path:
    config = tmp_path / "hooks-config.yaml"
    config.write_text(
        f"hooks:\n  pre-post:\n    - cmd: {pre}\n      with_text: true\n"
        f"  post-post:\n    - cmd: {post}\n      with_text: true\n",
        encoding="utf-8",
    )
    return config


def test_a_reply_payload_has_to_set_and_null_event_anchor_and_thread_fields(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    from tests import forgectl_harness as h

    body = tmp_path / "body.txt"
    body.write_text("thanks, fixed", encoding="utf-8")
    config = with_text_config(tmp_path, hooks.recorder("pre"), hooks.recorder("post"))
    forgectl.script(
        show(),
        ok("reply", h.post_doc("reply", dry_run=True, threaded=False)),
        ok("reply", h.post_doc("reply", ids=["gh:ic:IC_1"], threaded=False)),
    )
    code, doc, _ = cli(
        "reply", PR, "--to", "gh:ic:IC_9", "--body-file", str(body), "--json", "--actor", ACTOR,
        "--yes", "--config", str(config),
    )  # fmt: skip
    assert code == 0, doc
    for name, schema in (("pre", "pre-post"), ("post", "post-post")):
        payload = hooks.payload(name)
        validate(f"pr-comment/hooks/{schema}.json", payload)
        assert payload["action"] == "reply" and payload["to"] == "gh:ic:IC_9"
        assert payload["event"] is None and payload["anchor_head"] is None
        assert payload["thread_id"] is None and payload["thread_author_is_self"] is None
        assert payload["thread_path"] is None and payload["thread_line"] is None
        assert "thanks, fixed" in payload["bodies"][0]["text"]


def test_an_approve_payload_has_the_event_set_and_a_null_anchor(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks
) -> None:
    from tests import forgectl_harness as h

    config = with_text_config(tmp_path, hooks.recorder("pre"), hooks.recorder("post"))
    forgectl.script(
        show(),
        ok("approve", h.post_doc("approve", dry_run=True)),
        ok("approve", h.post_doc("approve", ids=["gh:r:R_1"], review_id="gh:r:R_1")),
    )
    code, doc, _ = cli(
        "approve", PR, "--expected-head", A, "--expected-base", "main", "--json", "--actor",
        ACTOR, "--yes", "--config", str(config),
    )  # fmt: skip
    assert code == 0, doc
    for name, schema in (("pre", "pre-post"), ("post", "post-post")):
        payload = hooks.payload(name)
        validate(f"pr-comment/hooks/{schema}.json", payload)
        assert payload["action"] == "approve" and payload["event"] == "approve"
        assert payload["anchor_head"] is None and payload["to"] is None
        assert payload["thread_id"] is None and payload["head_sha"] == A


# --- signals -----------------------------------------------------------------------------------


@pytest.mark.usefixtures("default_signals")
def test_a_signal_during_a_pre_post_hook_ends_the_run_before_any_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, hooks: Hooks, state_home: Path, pids: Path
) -> None:
    slow = hooks.script("sleep 60 & echo $! >> " + str(pids) + "; wait")
    with signal_self_when_started(pids, 2, signal.SIGTERM):
        code, doc, _ = blocked_run(cli, forgectl, tmp_path, "--hook", f"pre-post={slow}")
    assert code == 128 + signal.SIGTERM and doc is None
    assert forgectl.writes() == [] and audit(state_home) == []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(alive(int(p)) for p in pids.read_text().split()):
        time.sleep(0.05)
    assert not any(alive(int(p)) for p in pids.read_text().split())


@pytest.mark.usefixtures("default_signals")
def test_a_signal_between_two_post_post_hooks_stops_the_second_from_starting(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    hooks: Hooks,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = hooks.recorder("first")
    second = hooks.recorder("second")
    original = hooks_module.HookRunner.run_sync

    def run_then_signal(self: Any, hook: str, index: int, *rest: Any) -> Any:
        result = original(self, hook, index, *rest)
        if index == 0:  # the hook has ended; the signal lands before the next one starts
            os.kill(os.getpid(), signal.SIGTERM)
        return result

    monkeypatch.setattr(hooks_module.HookRunner, "run_sync", run_then_signal)
    code, doc, _ = review_run(
        cli, forgectl, tmp_path, "--hook", f"post-post={first}", "--hook", f"post-post={second}"
    )
    assert hooks.order() == ["first"] and not hooks.ran("second")
    assert any(
        e["event"] == "warning" and e["details"]["reason"] == "hook_skipped_after_signal"
        for e in log(state_home)
    ), (code, doc)
    assert len(forgectl.writes()) == 1 and audit(state_home)[1]["result"] == "done"
