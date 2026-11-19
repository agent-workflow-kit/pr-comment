"""The ``comment`` and ``reply`` orders' write (issue #30), through the real CLI against the fake
forgectl process (``tests/fakebin/forgectl``, pinned to forgectl's own schemas): the post, its audit
pair, the exit codes and what is never done (a retry, a rebuilt plan, a post without a record).

Every test runs with a private HOME and state directory; no real forgectl is ever called.
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

from pr_comment import cli as cli_module
from pr_comment.cli import main
from tests import forgectl_harness as h
from tests.forgectl_harness import (
    FakeForgectl,
    ProcessCall,
    assert_edge_rules,
    fail,
    ok,
)
from tests.posting import PR, A, B
from tests.schema_harness import validate
from tests.test_review_post import (
    _SLOW_ATTEMPTED,
    _SLOW_OUTCOME,
    _FailAt,
    _stable,
    audit,
    log,
    state_dir,
    validate_audit,
)

Run = Callable[..., tuple[int, Any, str]]

GL_PR = h.GITLAB_PR
ACTOR = "human:alice"
GH_TARGET = "gh:rc:RC_1"
GL_TARGET = "gl:d:" + "a" * 40
KINDS = ["comment", "reply"]


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


def dry(kind: str, pr: str = PR, **kw: Any) -> dict[str, Any]:
    return ok(kind, h.post_doc(kind, pr, dry_run=True, **kw))


def posted(kind: str, pr: str = PR, **kw: Any) -> dict[str, Any]:
    if pr == GL_PR:
        kw.setdefault("ids", ["gl:n:11"])
    else:
        kw.setdefault("ids", ["gh:ic:IC_1"])
    return ok(kind, h.post_doc(kind, pr, **kw))


def body_file(tmp_path: Path, text: str = "looks good to me") -> str:
    path = tmp_path / "body.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def args_for(
    kind: str, body: str, *extra: str, pr: str = PR, to: str | None = None
) -> tuple[str, ...]:
    target = (
        ("--to", to if to is not None else (GL_TARGET if pr == GL_PR else GH_TARGET))
        if kind == "reply"
        else ()
    )
    return (kind, pr, *target, "--body-file", body, "--json", "--actor", ACTOR, "--yes", *extra)


def writes_of(forgectl: FakeForgectl, kind: str) -> list[ProcessCall]:
    return [c for c in forgectl.calls() if c.subcommand == kind]


def sent(call: ProcessCall) -> str:
    assert call.stdin is not None
    return call.stdin.decode("utf-8")


def flag(call: ProcessCall, name: str) -> list[str]:
    return [a.split("=", 1)[1] for a in call.argv if a.startswith(name + "=")]


def check_edge(forgectl: FakeForgectl, tmp_path: Path, **kw: Any) -> None:
    assert_edge_rules(forgectl.calls(), root=tmp_path, **kw)


# --- the post ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_confirmed_text_post_sends_the_plan_byte_for_byte_and_audits_the_pair(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path, "thanks @amy, see the other thread")
    forgectl.script(show(), dry(kind), posted(kind))
    code, doc, _ = cli(*args_for(kind, body, "--decision-id", "d1"))
    assert code == 0, doc
    validate(f"pr-comment/cli/{kind}.json", doc)
    assert doc["action"] == kind and doc["dry_run"] is False and doc["plan"] is None
    assert doc["ids"] == ["gh:ic:IC_1"] and doc["already_posted"] is False
    assert doc["mentions_neutralized"] == ["amy"] and doc["warnings"] == []
    assert doc["head_sha"] == A and doc["base_ref"] == "main"
    if kind == "reply":
        assert doc["to"] == GH_TARGET and doc["threaded"] is True

    calls = [c for c in forgectl.calls() if c.subcommand != "--version"]
    assert [c.subcommand for c in calls] == ["show", kind, kind]
    pre, real = calls[1], calls[2]
    assert pre.is_dry_run and real.is_write
    # the real call carries exactly the plan the pre-check proved
    assert real.stdin == pre.stdin and real.stdin is not None
    assert "--yes" in real.argv and "--dry-run" not in real.argv
    assert flag(real, "--dedupe-key") == [doc["dedupe_key"]]
    assert flag(real, "--decision-id") == ["d1"] and flag(real, "--actor") == [ACTOR]
    assert flag(real, "--expected-head") == [] and flag(real, "--expected-base") == []
    if kind == "reply":
        assert flag(real, "--to") == [GH_TARGET]
    assert "@amy" not in sent(real) and "thanks" in sent(real)
    check_edge(forgectl, tmp_path, texts=[sent(real)], confirmed=True, confirmed_input=real.stdin)

    records = audit(state_home)
    validate_audit(records)
    attempted, outcome = records
    assert (attempted["result"], outcome["result"]) == ("attempted", "done")
    assert attempted["attempt_id"] == outcome["attempt_id"]
    assert attempted["action"] == kind and attempted["target"] == PR
    assert attempted["decision_id"] == ["d1"] and attempted["actor"] == ACTOR
    assert outcome["inputs"]["plan_sha256"] == doc["plan_sha256"]
    assert outcome["inputs"]["dedupe_key"] == doc["dedupe_key"]
    if kind == "reply":
        assert outcome["inputs"]["to"] == GH_TARGET
    (write,) = outcome["writes"]
    assert (write["index"], write["action"], write["result"]) == (0, kind, "done")
    assert write["lower"]["ids"] == ["gh:ic:IC_1"] and write["dedupe_key"] == doc["dedupe_key"]
    # no body in the record, only hashes
    text = (state_dir(state_home) / "audit.jsonl").read_text()
    assert "thanks" not in text and "other thread" not in text


@pytest.mark.parametrize("kind", KINDS)
def test_the_dedupe_key_sent_follows_the_body_and_the_reply_target(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, kind: str
) -> None:
    from pr_comment import keys

    pr_id = h.show_doc(PR)["pr_id"]
    first, second = "first body", "second body"
    other = "gh:rc:RC_2"
    runs = [(first, GH_TARGET), (second, GH_TARGET)]
    if kind == "reply":
        runs.append((first, other))
    forgectl.script(*[s for _ in runs for s in (show(), dry(kind), posted(kind))])
    for text, to in runs:
        code, _, _ = cli(*args_for(kind, body_file(tmp_path, text), to=to))
        assert code == 0
    sent_keys = [flag(c, "--dedupe-key") for c in writes_of(forgectl, kind) if c.is_write]
    expected = [
        [keys.reply_key(pr_id, to, text) if kind == "reply" else keys.comment_key(pr_id, text)]
        for text, to in runs
    ]
    assert sent_keys == expected
    assert len({k[0] for k in sent_keys}) == len(runs)


@pytest.mark.parametrize("kind", KINDS)
def test_a_gitlab_text_post(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"), dry(kind, GL_PR), posted(kind, GL_PR, thread_ids=[])
    )
    code, doc, _ = cli(*args_for(kind, body, pr=GL_PR))
    assert code == 0, doc
    validate(f"pr-comment/cli/{kind}.json", doc)
    assert doc["ids"] == ["gl:n:11"] and doc["review_id"] is None
    if kind == "reply":
        assert doc["to"] == GL_TARGET
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert len(forgectl.writes()) == 1


def test_a_reply_that_joined_no_thread_says_so(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry("reply", threaded=False), posted("reply", threaded=False))
    code, doc, err = cli(*args_for("reply", body, to="gh:ic:IC_9"))
    assert code == 0, doc
    validate("pr-comment/cli/reply.json", doc)
    assert doc["threaded"] is False
    assert audit(state_home)[1]["result"] == "done"


def test_the_human_output_names_what_was_posted(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry("reply", threaded=False), posted("reply", threaded=False))
    args = [a for a in args_for("reply", body, to="gh:ic:IC_9") if a != "--json"]
    code, _, err = cli(*args)
    assert code == 0
    assert "posted: reply to gh:ic:IC_9 on" in err and "not threaded" in err
    assert "gh:ic:IC_1" in err


@pytest.mark.parametrize("kind", KINDS)
def test_already_posted_is_success_and_is_recorded_as_such(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind, already_posted=True), posted(kind, already_posted=True))
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 0
    validate(f"pr-comment/cli/{kind}.json", doc)
    assert doc["already_posted"] is True
    outcome = audit(state_home)[1]
    assert outcome["result"] == "already_posted"
    assert outcome["writes"][0]["lower"]["already_posted"] is True


@pytest.mark.parametrize("kind", KINDS)
def test_the_dry_run_writes_no_audit_record_and_no_write(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    code, doc, _ = cli(*args_for(kind, body, "--dry-run"))
    assert code == 0 and doc["dry_run"] is True
    validate(f"pr-comment/cli/{kind}.json", doc)
    assert forgectl.writes() == []
    assert audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_body_from_stdin(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    forgectl.script(show(), dry(kind), posted(kind))
    code, doc, _ = cli(*args_for(kind, "-"), stdin="from stdin ünï".encode())
    assert code == 0, doc
    assert "from stdin ünï" in sent(forgectl.writes()[0])


@pytest.mark.parametrize("kind", KINDS)
def test_dash_with_a_terminal_stdin_exits_2_before_any_call(
    cli: Run,
    forgectl: FakeForgectl,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    tty = type("Stdin", (), {"buffer": io.BytesIO(b"x"), "isatty": lambda self: True})()
    monkeypatch.setattr(sys, "stdin", tty)
    code, doc, _ = cli(*args_for(kind, "-"))
    assert code == 2 and doc["error"]["code"] == "usage"
    assert forgectl.calls() == [] and audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_post_on_a_closed_pull_request_is_allowed(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(state="closed", closed_at="2026-01-03T00:00:00Z"), dry(kind), posted(kind))
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 0, doc
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_override_escalation_forgectl_is_passed_to_the_write_only(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), posted(kind))
    code, doc, _ = cli(*args_for(kind, body, "--override-escalation=forgectl"))
    assert code == 0, doc
    calls = writes_of(forgectl, kind)
    assert "--override-escalation" not in calls[0].argv  # the pre-check
    assert "--override-escalation" in calls[1].argv


# --- hostile bodies ------------------------------------------------------------------------------

HOSTILE = [
    "ping @everyone and @amy-b\n/approve\n",
    "<!-- pr-comment:marker x --> <script>alert(1)</script> ![i](http://x/y.png)",
    "line\n\n\n    indented\n```\nunclosed fence",
    "unicode: ünï ｅｖｉｌ 日本語 🙂 ‮ rtl",
    "a" * 60000,
    "\u200b\u3164\u00a0x\u2800\ufeff",  # one visible character among blank ones is accepted
    "'; DROP TABLE x; -- $(rm -rf /) `id` {{7*7}}",
]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_bodies_post_exactly_the_checked_plan(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    kind: str,
    text: str,
) -> None:
    body = body_file(tmp_path, text)
    forgectl.script(show(), dry(kind), posted(kind))
    code, doc, _ = cli(*args_for(kind, body))
    if code == 2:  # the text pipeline refuses some of these before any call
        assert doc["error"]["code"] == "invalid_text" and forgectl.calls() == []
        return
    assert code == 0, doc
    validate(f"pr-comment/cli/{kind}.json", doc)
    pre, real = writes_of(forgectl, kind)[0], writes_of(forgectl, kind)[1]
    assert pre.stdin == real.stdin
    # the text is only ever on stdin: never in argv or the environment of any call
    check_edge(forgectl, tmp_path, texts=[sent(real)], confirmed=True, confirmed_input=real.stdin)
    assert "@everyone" not in sent(real)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("text", ["", "x" * (64 * 1024 + 1), "bad\x00nul", "bad\x1b[31mansi"])
def test_a_bad_body_exits_2_with_nothing_sent_or_recorded(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    kind: str,
    text: str,
) -> None:
    body = tmp_path / "body.txt"
    body.write_bytes(text.encode("utf-8"))
    code, doc, _ = cli(*args_for(kind, str(body)))
    assert code == 2
    validate("pr-comment/cli/error.json", doc)
    assert forgectl.calls() == [] and audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "text",
    [
        "\n",
        "\r\n",
        "  ",
        "  \n\n",
        "\x0b",
        "\u0085",
        "  \t",
        "\u00a0",
        "\u200b",
        "\ufeff",
        "\u3000",
        "\u3164",
        "\u2800",
        "\u200b\u3164\u2800 \n\u00a0",
    ],
)
def test_a_whitespace_only_body_is_empty_and_exits_2_with_nothing_sent_or_recorded(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str, text: str
) -> None:
    body = tmp_path / "body.txt"
    body.write_bytes(text.encode("utf-8"))
    code, doc, _ = cli(*args_for(kind, str(body)))
    assert code == 2
    validate("pr-comment/cli/error.json", doc)
    assert "empty" in doc["error"]["message"]
    assert forgectl.calls() == [] and audit(state_home) == []


# --- exit codes: forgectl's answers pass through with the write's own landed and retry ----------


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    ("code_name", "extra"),
    [
        ("rate_limited", {"retry_after": 7}),
        ("network_error", {}),
        ("upstream_error", {}),
        ("auth_required", {}),
        ("permission_denied", {}),
    ],
)
def test_a_host_failure_of_the_write_is_exit_12_not_landed_and_never_retried(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    kind: str,
    code_name: str,
    extra: dict[str, Any],
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), fail(kind, code_name, phase="write", **extra))
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == code_name and error["landed"] is False
    assert error["retry"] == "same_command" and error["lower_error"]["code"] == code_name
    assert len(writes_of(forgectl, kind)) == 2  # the pre-check and one write: never a second
    assert len(forgectl.writes()) == 1
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "failed"
    assert records[1]["writes"][0]["lower"]["code"] == code_name


@pytest.mark.parametrize("kind", KINDS)
def test_outcome_unknown_is_reported_unknown_and_not_retried(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), fail(kind, "outcome_unknown"))
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert error["landed"] is None and error["retry"] == "same_command"
    assert len(forgectl.writes()) == 1
    records = audit(state_home)
    assert records[1]["result"] == "outcome_unknown"
    assert records[1]["writes"][0]["lower"]["code"] == "outcome_unknown"


@pytest.mark.parametrize("kind", KINDS)
def test_a_rerun_after_outcome_unknown_reuses_the_key_and_is_already_posted(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        fail(kind, "outcome_unknown"),
        show(),
        dry(kind, already_posted=True),
        posted(kind, already_posted=True),
    )
    code, first, _ = cli(*args_for(kind, body))
    assert code == 12 and first["error"]["code"] == "outcome_unknown"
    code, second, _ = cli(*args_for(kind, body))
    assert code == 0 and second["already_posted"] is True
    writes = forgectl.writes()
    assert len(writes) == 2
    assert flag(writes[0], "--dedupe-key") == flag(writes[1], "--dedupe-key")
    assert flag(writes[0], "--dedupe-key") == [second["dedupe_key"]]
    assert writes[0].stdin == writes[1].stdin
    records = audit(state_home)
    assert [r["result"] for r in records] == [
        "attempted",
        "outcome_unknown",
        "attempted",
        "already_posted",
    ]
    assert records[0]["attempt_id"] != records[2]["attempt_id"]


@pytest.mark.parametrize("kind", KINDS)
def test_quick_action_ran_landed_and_is_not_retried(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(pr=GL_PR, backend="gitlab"),
        dry(kind, GL_PR),
        fail(kind, "quick_action_ran", details={"ids": ["gl:n:11"]}, phase="write"),
    )
    code, doc, _ = cli(*args_for(kind, body, pr=GL_PR))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "quick_action_ran"
    assert (error["landed"], error["retry"]) == (True, "none")
    records = audit(state_home)
    validate_audit(records)
    assert records[1]["result"] == "partial"
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_forgectls_own_audit_failure_passes_through_with_its_writes_pair(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(), dry(kind), fail(kind, "audit_failed", details={"outcome": "posted"}, phase="write")
    )
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "audit_failed" and (error["landed"], error["retry"]) == (True, "none")
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_veto_of_forgectls_hook_passes_through_as_exit_11(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(), dry(kind), fail(kind, "vetoed", hook="pre-post", hook_stderr="no", phase="write")
    )
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 11 and doc["error"]["code"] == "vetoed"
    assert audit(state_home)[1]["result"] == "failed"
    assert [e["level"] for e in log(state_home) if e["event"] == "vetoed"] == ["warn"]


@pytest.mark.parametrize("kind", KINDS)
def test_an_escalation_of_forgectls_hook_passes_through_as_exit_13_and_is_logged(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        fail(
            kind,
            "escalated",
            hook="pre-post",
            hook_stderr="",
            escalated_hooks=[{"hook": "pre-post", "index": 0}],
            phase="write",
        ),
    )
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 13 and doc["error"]["code"] == "escalated"
    assert [e["level"] for e in log(state_home) if e["event"] == "escalated"] == ["warn"]


# --- refusals before the write -------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_an_expected_head_that_moved_refuses_before_any_pre_check_or_audit_record(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(head=B))
    code, doc, _ = cli(*args_for(kind, body, "--expected-head", A))
    assert code == 20 and doc["error"]["reason"] == "stale_head"
    assert doc["error"]["details"]["head_sha"] == B
    assert [c.subcommand for c in forgectl.calls()] == ["show"]
    assert audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
def test_expect_plan_mismatch_refuses_before_the_audit_attempt(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    code, doc, _ = cli(*args_for(kind, body, "--expect-plan", "0" * 64))
    assert code == 20 and doc["error"]["reason"] == "plan_changed"
    assert forgectl.writes() == [] and audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_matching_expect_plan_posts(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), show(), dry(kind), posted(kind))
    _, preview, _ = cli(*args_for(kind, body, "--dry-run"))
    code, doc, _ = cli(*args_for(kind, body, "--expect-plan", preview["plan_sha256"]))
    assert code == 0 and doc["plan_sha256"] == preview["plan_sha256"]


def test_an_unknown_reply_target_exits_10_before_any_audit_record(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), fail("reply", "not_found", phase="read"))
    code, doc, _ = cli(*args_for("reply", body, to="gh:rc:RC_nope"))
    assert code == 10 and doc["error"]["details"]["what"] == "to"
    assert forgectl.writes() == [] and audit(state_home) == []


@pytest.mark.parametrize("target", ["gl:d:xyz", "x", "gh:rc:", "", "gh:rc:a b", "GH:RC:1"])
def test_a_reply_target_with_a_bad_grammar_exits_2_before_any_call(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, target: str
) -> None:
    body = body_file(tmp_path)
    code, doc, _ = cli(*args_for("reply", body, to=target))
    assert code == 2 and doc["error"]["code"] == "usage"
    assert forgectl.calls() == [] and audit(state_home) == []


# --- the strict answer ---------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["missing", None, "yes"])
def test_a_reply_answer_without_threaded_is_a_contract_mismatch_that_landed(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: object,
) -> None:
    from pr_comment import gateway as gateway_module

    body = body_file(tmp_path)
    forgectl.script(show(), dry("reply"), posted("reply"))
    original = gateway_module.ProcessGateway.call

    def call(self: Any, subcommand: str, args: Any = (), *, stdin: bytes | None = None) -> Any:
        result = original(self, subcommand, args, stdin=stdin)
        if subcommand == "reply" and "--yes" in args:
            document = json.loads(result.stdout)
            if change == "missing":
                document.pop("threaded", None)
            else:
                document["threaded"] = change
            result = dataclasses.replace(result, stdout=json.dumps(document).encode())
        return result

    monkeypatch.setattr(gateway_module.ProcessGateway, "call", call)
    code, doc, _ = cli(*args_for("reply", body))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")
    records = audit(state_home)
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert len(forgectl.writes()) == 1


# --- audit failures ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_attempted_append_sends_nothing(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    _FailAt(1).install(monkeypatch)
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12 and doc["error"]["code"] == "audit_unavailable"
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (False, "same_command")
    assert forgectl.writes() == []


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_outcome_append_after_a_landed_write_is_audit_failed_and_not_a_retry(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), posted(kind))
    _FailAt(2).install(monkeypatch)
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (True, "none")
    assert error["details"]["outcome"]["ids"] == ["gh:ic:IC_1"]
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_failed_outcome_append_after_a_failed_write_keeps_the_writes_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), fail(kind, "outcome_unknown"))
    _FailAt(2).install(monkeypatch)
    code, doc, _ = cli(*args_for(kind, body))
    error = doc["error"]
    assert code == 12 and error["code"] == "audit_failed"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert error["details"]["outcome"]["error"]["code"] == "outcome_unknown"


@pytest.mark.parametrize("kind", KINDS)
def test_an_unwritable_audit_file_ends_the_run_before_any_forgectl_call(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    directory = state_dir(state_home)
    directory.mkdir(parents=True, mode=0o700)
    for parent in (state_home, directory):
        parent.chmod(0o700)
    (directory / "tmp").mkdir(mode=0o700)
    target = directory / "audit.jsonl"
    target.write_text("")
    target.chmod(0o666)
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12 and doc["error"]["code"] == "audit_unavailable"
    assert forgectl.calls() == []


# --- the dialog ----------------------------------------------------------------------------------


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], list[int]]:
    asked: list[int] = []

    def setup(answer: bool) -> list[int]:
        monkeypatch.setattr(cli_module, "stream_is_tty", lambda stream: True)

        def confirm(prepared: Any, **kw: Any) -> bool:
            asked.append(1)
            return answer

        monkeypatch.setattr(cli_module, "confirm", confirm)
        return asked

    return setup


@pytest.mark.parametrize("kind", KINDS)
def test_a_declined_dialog_posts_nothing_and_writes_no_audit_record(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    interactive: Callable[[bool], list[int]],
    kind: str,
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    asked = interactive(False)
    args = [a for a in args_for(kind, body) if a != "--yes"]
    code, doc, _ = cli(*args)
    assert code == 13 and doc["error"]["message"] == "declined"
    assert asked == [1] and forgectl.writes() == [] and audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
def test_an_accepted_dialog_posts_what_the_dialog_showed(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    interactive: Callable[[bool], list[int]],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), posted(kind))
    interactive(True)
    shown: list[Any] = []

    def confirm(prepared: Any, **kw: Any) -> bool:
        shown.append((prepared.plan.input, prepared.plan.command()))
        return True

    monkeypatch.setattr(cli_module, "confirm", confirm)
    args = [a for a in args_for(kind, body) if a != "--yes"]
    code, doc, _ = cli(*args)
    assert code == 0 and len(shown) == 1
    real = writes_of(forgectl, kind)[1]
    assert real.stdin == shown[0][0]  # the bytes the human confirmed
    assert [r["result"] for r in audit(state_home)] == ["attempted", "done"]


@pytest.mark.parametrize("kind", KINDS)
def test_a_non_interactive_run_without_yes_posts_nothing_and_records_nothing(
    cli: Run, forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    args = [a for a in args_for(kind, body) if a != "--yes"]
    code, doc, _ = cli(*args)
    assert code == 13 and doc["error"]["code"] == "confirmation_required"
    assert forgectl.writes() == [] and audit(state_home) == []


# --- signals and the deadline --------------------------------------------------------------------


def _start_cli(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, *argv: str
) -> subprocess.Popen[bytes]:
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home)})
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return subprocess.Popen(
        [sys.executable, "-m", "pr_comment", *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_during_the_write_is_outcome_unknown_and_audited(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str, signum: int
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        ok(
            kind,
            h.post_doc(kind, ids=["gh:ic:IC_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    forgectl.allow_unused_steps = False
    n = forgectl.next_call_number() + 3  # show, the pre-check, --version, the write
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(kind, body))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signum)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    doc = json.loads(stdout)
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]
    assert records[1]["writes"][0]["lower"]["code"] == "outcome_unknown"
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_signal_after_forgectl_printed_its_full_answer_is_still_outcome_unknown(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        ok(kind, h.post_doc(kind, ids=["gh:ic:IC_1"]), hang={"phase": "after_outcome"}),
    )
    n = forgectl.next_call_number() + 3
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(kind, body))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signal.SIGTERM)  # the exact pid this test started
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 12
    doc = json.loads(stdout)
    validate("pr-comment/cli/error.json", doc)
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    records = audit(state_home)
    validate_audit(records)
    # forgectl's own answer was a success: the record keeps it, the run still ends unknown
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert records[0]["attempt_id"] == records[1]["attempt_id"]
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
def test_a_signal_before_the_write_exits_128_plus_n_and_writes_no_record(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str
) -> None:
    body = body_file(tmp_path)
    forgectl.script(ok("show", h.show_doc(PR), hang={"phase": "before_write"}))
    n = forgectl.next_call_number()
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for(kind, body))
    forgectl.wait_ready(n)
    os.kill(proc.pid, signal.SIGTERM)
    stdout, _ = proc.communicate(timeout=60)
    assert proc.returncode == 143 and stdout == b""
    assert audit(state_home) == []


@pytest.mark.parametrize("kind", KINDS)
def test_the_deadline_of_a_write_is_outcome_unknown_and_never_retried(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from pr_comment import gateway

    monkeypatch.setattr(gateway, "DEADLINE_SLACK_SECONDS", 0.0)
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "1s")
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        ok(kind, h.post_doc(kind, ids=["gh:ic:IC_1"]), hang={"phase": "after_write_attempt"}),
    )
    forgectl.allow_fake_errors = True  # a call that is killed has no end event
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert len(forgectl.writes()) == 1
    assert audit(state_home)[1]["result"] == "outcome_unknown"


@pytest.mark.parametrize("kind", KINDS)
def test_the_deadline_during_a_pre_write_hook_is_outcome_unknown_never_a_safe_rerun(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    # a forgectl `pre-post` hook that outlasts the deadline: forgectl is killed before its own
    # write, but from outside that cannot be told from a kill after it, so the run is unknown (#93)
    from pr_comment import gateway

    monkeypatch.setattr(gateway, "DEADLINE_SLACK_SECONDS", 0.0)
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "4s")  # the reads before the write fit in it
    # todo(#94): this deadline also bounds the show and dry-run calls; a load stall there reads
    # as network_error
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        ok(kind, h.post_doc(kind, ids=["gh:ic:IC_1"]), hang={"phase": "before_write"}),
    )
    forgectl.allow_fake_errors = True  # a call that is killed has no end event
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")
    assert audit(state_home)[1]["result"] == "outcome_unknown"


@pytest.mark.parametrize("kind", KINDS)
def test_a_deadline_before_a_slow_forgectl_answers_network_error_is_outcome_unknown(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    # forgectl's own network_error answer comes after the deadline kill here, so nothing was printed
    # (the fake prints after its delay); the printed-envelope case is the unit test
    # `test_a_killed_write_with_a_network_error_envelope_printed_stays_outcome_unknown`
    from pr_comment import gateway

    monkeypatch.setattr(gateway, "DEADLINE_SLACK_SECONDS", 0.0)
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "4s")  # the reads before the write fit in it
    # todo(#94): this deadline also bounds the show and dry-run calls; a load stall there reads
    # as network_error
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry(kind),
        fail(kind, "network_error", phase="write", delay=10),
    )
    forgectl.allow_fake_errors = True
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12
    error = doc["error"]
    assert error["code"] == "outcome_unknown"
    assert (error["landed"], error["retry"]) == (None, "same_command")


def test_attempted_is_on_disk_while_forgectl_runs(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path
) -> None:
    body = body_file(tmp_path)
    forgectl.script(
        show(),
        dry("comment"),
        ok(
            "comment",
            h.post_doc("comment", ids=["gh:ic:IC_1"]),
            hang={"phase": "after_write_attempt"},
        ),
    )
    n = forgectl.next_call_number() + 3
    proc = _start_cli(forgectl, tmp_path, state_home, *args_for("comment", body))
    forgectl.wait_ready(n)
    records = audit(state_home)
    os.kill(proc.pid, signal.SIGTERM)  # the exact pid this test started
    proc.communicate(timeout=60)
    assert [r["result"] for r in records] == ["attempted"]


def _slow_launcher(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, source: str, *argv: str
) -> tuple[subprocess.Popen[bytes], Path]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    launcher = scripts / "slow.py"
    launcher.write_text(source, encoding="utf-8")
    ready = tmp_path / "append-started"
    env = forgectl.environ({"XDG_STATE_HOME": str(state_home), "PRC_TEST_READY": str(ready)})
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    proc = subprocess.Popen(
        [sys.executable, str(launcher), *argv],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,
    )
    return proc, ready


def _wait_for(ready: Path, proc: subprocess.Popen[bytes]) -> None:
    import time

    deadline = time.monotonic() + 30
    while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ready.exists(), "the append never started"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_while_the_outcome_is_appended_waits_for_the_record(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str, signum: int
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), posted(kind))
    proc, ready = _slow_launcher(
        forgectl, tmp_path, state_home, _SLOW_OUTCOME, *args_for(kind, body)
    )
    try:
        _wait_for(ready, proc)
        os.kill(proc.pid, signum)  # the exact pid this test started
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    # the write landed and its answer was known: the record is complete and the run ends with it
    assert proc.returncode == 0, stderr
    doc = json.loads(stdout)
    validate(f"pr-comment/cli/{kind}.json", doc)
    assert doc["ids"] == ["gh:ic:IC_1"]
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "done"]
    assert len(forgectl.writes()) == 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_while_attempted_is_appended_sends_no_write(
    forgectl: FakeForgectl, tmp_path: Path, state_home: Path, kind: str, signum: int
) -> None:
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    proc, ready = _slow_launcher(
        forgectl, tmp_path, state_home, _SLOW_ATTEMPTED, *args_for(kind, body)
    )
    try:
        _wait_for(ready, proc)
        os.kill(proc.pid, signum)  # the exact pid this test started
        stdout, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 128 + signum, stderr
    assert stdout == b"" and forgectl.writes() == []
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "failed"]
    assert records[1]["attempt_id"] == records[0]["attempt_id"]
    # an expected interruption, not an internal error: exit slot 1, code `interrupted`
    (write,) = records[1]["writes"]
    assert (write["lower"]["exit"], write["lower"]["code"]) == (1, "interrupted")


# --- an unexpected error after ``attempted`` -----------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_an_unexpected_error_of_the_write_call_is_outcome_unknown_with_a_closed_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from pr_comment import gateway as gateway_module

    original = gateway_module.ProcessGateway.call

    def call(self: Any, subcommand: str, args: Any = (), *, stdin: bytes | None = None) -> Any:
        if subcommand == kind and "--yes" in args:
            raise RuntimeError("boom")
        return original(self, subcommand, args, stdin=stdin)

    monkeypatch.setattr(gateway_module.ProcessGateway, "call", call)
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind))
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12 and doc["error"]["code"] == "outcome_unknown"
    assert (doc["error"]["landed"], doc["error"]["retry"]) == (None, "same_command")
    records = audit(state_home)
    validate_audit(records)
    assert [r["result"] for r in records] == ["attempted", "outcome_unknown"]


@pytest.mark.parametrize("kind", KINDS)
def test_an_unexpected_error_after_the_write_is_outcome_unknown_with_a_closed_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from pr_comment import post as post_module

    def broken(*args: Any, **kw: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(post_module, "result_document", broken)
    body = body_file(tmp_path)
    forgectl.script(show(), dry(kind), posted(kind))
    code, doc, _ = cli(*args_for(kind, body))
    assert code == 12 and doc["error"]["code"] == "outcome_unknown"
    assert len(forgectl.writes()) == 1  # never posted again
    assert [r["result"] for r in audit(state_home)] == ["attempted", "outcome_unknown"]


# --- goldens -------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
def test_goldens_of_the_text_result_its_dry_run_and_the_audit_pair(
    cli: Run,
    forgectl: FakeForgectl,
    tmp_path: Path,
    state_home: Path,
    golden_json: Callable[[str, Any], None],
    kind: str,
) -> None:
    body = body_file(tmp_path, "Thanks @amy, done.\n\nSee the follow-up.")
    forgectl.script(show(), dry(kind), show(), dry(kind), posted(kind))
    code, preview, _ = cli(*args_for(kind, body, "--dry-run"))
    assert code == 0
    validate(f"pr-comment/cli/{kind}.json", preview)
    code, result, _ = cli(*args_for(kind, body))
    assert code == 0
    validate(f"pr-comment/cli/{kind}.json", result)
    assert preview["plan_sha256"] == result["plan_sha256"]
    assert preview["dedupe_key"] == result["dedupe_key"]
    records = audit(state_home)
    validate_audit(records)
    golden_json(f"{kind}/result.json", _stable(result))
    golden_json(f"{kind}/dry-run.json", _stable({**preview, "plan": None}))
    golden_json(f"{kind}/audit-pair.json", _stable(records))
