"""Audit record pairs (issue #11): schema validity, pairing, results, failure handling."""

from __future__ import annotations

import errno
import json
import os
import stat
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pr_comment import __version__, appendfile, audit, errors, files
from pr_comment.actor import RunContext
from pr_comment.audit import (
    AuditFailedError,
    AuditInputs,
    AuditTrail,
    AuditUnavailableError,
    LowerResult,
    RunRecord,
    WriteRecord,
    aggregate_result,
    audit_failed_error,
    classify_write,
)
from tests.schema_harness import validate

SHA = "a" * 40
SHA256 = "b" * 64
TS = "2026-11-17T10:00:00.000Z"
BODY_TEXT = "TOP-SECRET-BODY-TEXT <script>alert(1)</script> ‮"


@pytest.fixture
def dirs(tmp_path: Path) -> Iterator[files.StateDirs]:
    (tmp_path / "home").mkdir()
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg")}
    with files.open_state_dirs(env) as opened:
        yield opened


def audit_path(dirs: files.StateDirs) -> Path:
    return dirs.state_path / "audit.jsonl"


def read_records(dirs: files.StateDirs) -> list[dict[str, Any]]:
    raw = audit_path(dirs).read_bytes()
    assert raw.endswith(b"\n") or raw == b""
    return [json.loads(line) for line in raw.decode("ascii").splitlines()]


def inputs(**overrides: Any) -> AuditInputs:
    base: dict[str, Any] = {
        "event": "comment",
        "draft_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "draft_sha256": SHA256,
        "anchor_head": SHA,
        "reanchor_to": None,
        "pinned_head": SHA,
        "base_ref": "main",
        "dedupe_key": "k1",
        "to": None,
        "threads": (),
        "bodies": ({"role": "body", "sha256": SHA256, "bytes": 12},),
        "counts": None,
        "plan_sha256": SHA256,
        "allowed_mentions": (),
        "override_escalation": (),
        "config_hash": SHA256,
        "config_path": None,
    }
    base.update(overrides)
    if base["counts"] is None:
        base["counts"] = zero_counts()
    return AuditInputs(**base)


def zero_counts() -> dict[str, Any]:
    from tests.schema_harness import load_schemas

    def zero(schema: dict[str, Any]) -> Any:
        if schema.get("type") == "object":
            return {k: zero(v) for k, v in schema["properties"].items()}
        return 0

    return zero(load_schemas()["pr-comment/defs.json"]["$defs"]["counts"])


def run_ctx(**overrides: Any) -> RunContext:
    base: dict[str, Any] = {
        "actor": "auto:bot",
        "actor_source": "flag",
        "stdin_tty": False,
        "interactive": False,
    }
    base.update(overrides)
    return RunContext(**base)


def run_record(**overrides: Any) -> RunRecord:
    base: dict[str, Any] = {
        "run": run_ctx(),
        "decision_ids": (),
        "action": "comment",
        "target": "example.test/o/r#7",
        "pr_id": "gh:pr:abc123",
        "inputs": inputs(),
    }
    base.update(overrides)
    return RunRecord(**base)


def lower(**overrides: Any) -> LowerResult:
    base: dict[str, Any] = {
        "exit": 0,
        "code": None,
        "reason": None,
        "ids": ("gh:ic:x1",),
        "thread_ids": (),
        "review_id": None,
        "already_posted": False,
    }
    base.update(overrides)
    return LowerResult(**base)


def done_write(index: int = 0, **overrides: Any) -> WriteRecord:
    base: dict[str, Any] = {
        "index": index,
        "ts": TS,
        "action": "comment",
        "thread_id": None,
        "dedupe_key": "k1",
        "lower": lower(),
    }
    base.update(overrides)
    return WriteRecord.from_lower(**base)


def full_run(dirs: files.StateDirs, writes: list[WriteRecord] | None = None) -> AuditTrail:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    trail.outcome(writes or [done_write()])
    trail.close()
    return trail


# --- the pair -------------------------------------------------------------------------------


def test_a_pair_validates_against_the_schema_and_shares_the_attempt_id(
    dirs: files.StateDirs,
) -> None:
    trail = full_run(dirs)
    first, second = read_records(dirs)
    validate("pr-comment/audit.json", first)
    validate("pr-comment/audit.json", second)
    assert first["result"] == "attempted"
    assert second["result"] == "done"
    assert first["attempt_id"] == second["attempt_id"] == trail.attempt_id
    assert first["version"] == second["version"] == __version__
    assert first["tool"] == "pr-comment"


def test_the_attempted_record_has_no_writes_and_the_outcome_has_hook_decision_ids(
    dirs: files.StateDirs,
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    trail.outcome([done_write()], hook_decision_ids=["h-1", "h-2"])
    first, second = read_records(dirs)
    assert "writes" not in first and "hook_decision_id" not in first
    assert second["hook_decision_id"] == ["h-1", "h-2"]
    assert second["writes"][0]["lower"]["ids"] == ["gh:ic:x1"]
    assert first["inputs"] == second["inputs"]


def test_run_fields_come_from_the_run_context(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    ctx = run_ctx(actor="human:alice", actor_source="tty", stdin_tty=True, interactive=True)
    trail.attempted(run_record(run=ctx, decision_ids=("d-1", "d-2")))
    (rec,) = read_records(dirs)
    assert (rec["actor"], rec["actor_source"], rec["stdin_tty"]) == ("human:alice", "tty", True)
    assert rec["decision_id"] == ["d-1", "d-2"]
    validate("pr-comment/audit.json", rec)


def test_override_is_derived_from_the_inputs(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    both = inputs(override_escalation=("forgectl", "pr-comment"), allowed_mentions=("bob",))
    trail.attempted(run_record(run=run_ctx(actor="human:me"), inputs=both))
    (rec,) = read_records(dirs)
    assert rec["override"] == ["override_escalation", "allow_mention"]
    assert rec["inputs"]["allowed_mentions"] == ["bob"]
    assert rec["inputs"]["override_escalation"] == ["forgectl", "pr-comment"]
    validate("pr-comment/audit.json", rec)


def test_no_override_gives_an_empty_list(dirs: files.StateDirs) -> None:
    AuditTrail.open(dirs).attempted(run_record())
    (rec,) = read_records(dirs)
    assert rec["override"] == []


def test_config_hash_and_path_are_recorded(dirs: files.StateDirs) -> None:
    AuditTrail.open(dirs).attempted(
        run_record(inputs=inputs(config_hash="c" * 64, config_path="/x/config.yaml"))
    )
    (rec,) = read_records(dirs)
    assert rec["inputs"]["config_hash"] == "c" * 64
    assert rec["inputs"]["config_path"] == "/x/config.yaml"


def test_every_action_and_input_shape_validates(dirs: files.StateDirs) -> None:
    for action, extra in [
        ("review", {"event": "request_changes", "reanchor_to": SHA}),
        ("approve", {"event": "approve"}),
        ("reply", {"to": "gh:ic:abc", "event": None}),
        (
            "resolve",
            {
                "event": None,
                "threads": ("gh:rt:aaa", "gh:rt:bbb"),
                "bodies": (),
                "dedupe_key": None,
                "plan_sha256": None,
                "base_ref": None,
                "draft_id": None,
                "draft_sha256": None,
                "anchor_head": None,
            },
        ),
    ]:
        trail = AuditTrail.open(dirs)
        trail.attempted(run_record(action=action, inputs=inputs(**extra)))
        trail.outcome([done_write(action=action)])
        trail.close()
    for rec in read_records(dirs):
        validate("pr-comment/audit.json", rec)


def test_each_run_gets_a_new_attempt_id(dirs: files.StateDirs) -> None:
    ids = {AuditTrail.open(dirs).attempt_id for _ in range(50)}
    assert len(ids) == 50
    for attempt_id in ids:
        validate("pr-comment/audit.json#/properties/attempt_id", attempt_id)


def test_the_file_is_created_with_mode_0600_and_appended_to(dirs: files.StateDirs) -> None:
    full_run(dirs)
    full_run(dirs)
    assert stat.S_IMODE(os.lstat(audit_path(dirs)).st_mode) == 0o600
    assert len(read_records(dirs)) == 4


# --- ordering ----------------------------------------------------------------------------------


def test_an_outcome_without_attempted_is_a_programming_error(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    with pytest.raises(ValueError, match="attempted"):
        trail.outcome([done_write()])
    assert audit_path(dirs).read_bytes() == b""


def test_attempted_twice_and_outcome_twice_are_programming_errors(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    with pytest.raises(ValueError):
        trail.attempted(run_record())
    trail.outcome([done_write()])
    with pytest.raises(ValueError):
        trail.outcome([done_write()])
    assert len(read_records(dirs)) == 2


def test_writes_must_be_numbered_from_zero_without_gaps(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    for bad in (
        [],
        [done_write(1)],
        [done_write(0), done_write(2)],
        [done_write(0), done_write(0)],
    ):
        with pytest.raises(ValueError):
            trail.outcome(bad)
    assert len(read_records(dirs)) == 1


def test_a_run_whose_writes_were_all_blocked_before_forgectl_writes_no_pair(
    dirs: files.StateDirs,
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    blocked = WriteRecord.blocked(0, TS, "comment", None, "k1", "vetoed")
    with pytest.raises(ValueError, match="forgectl"):
        trail.outcome([blocked])


def test_a_veto_after_a_landed_write_is_recorded_as_partial(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    result = trail.outcome(
        [done_write(0), WriteRecord.blocked(1, TS, "resolve", "gh:rt:aaa", None, "vetoed")]
    )
    assert result == "partial"
    rec = read_records(dirs)[1]
    assert rec["writes"][1]["lower"] is None
    assert rec["result"] == "partial"
    validate("pr-comment/audit.json", rec)


def test_a_veto_before_the_first_write_is_listed_in_the_outcome(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    result = trail.outcome(
        [
            WriteRecord.blocked(0, TS, "comment", None, "k1", "escalated"),
            done_write(1),
        ]
    )
    assert result == "partial"
    validate("pr-comment/audit.json", read_records(dirs)[1])


# --- results ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "done"),
        ({"already_posted": True}, "already_posted"),
        ({"already_posted": None}, "done"),
        ({"exit": 20, "code": "stale_head"}, "refused"),
        ({"exit": 20, "code": "refused", "reason": "stale_head"}, "refused"),
        ({"exit": 12, "code": "outcome_unknown"}, "outcome_unknown"),
        ({"exit": 12, "code": "outcome_partial"}, "partial"),
        ({"exit": 12, "code": "quick_action_ran"}, "partial"),
        ({"exit": 12, "code": "approved_stale_head"}, "partial"),
        ({"exit": 12, "code": "audit_failed"}, "partial"),
        ({"exit": 12, "code": "network_error"}, "failed"),
        ({"exit": 12, "code": None}, "failed"),
        ({"exit": 1, "code": "internal"}, "failed"),
        ({"exit": 10, "code": "not_found"}, "failed"),
        ({"exit": 2, "code": "usage"}, "failed"),
        ({"exit": 13, "code": "escalated"}, "failed"),
    ],
)
def test_a_write_result_follows_forgectls_exit_and_code(
    kwargs: dict[str, Any], expected: str
) -> None:
    assert classify_write(lower(**kwargs)) == expected


AGGREGATION = [
    (["done"], "done"),
    (["already_posted"], "already_posted"),
    (["partial"], "partial"),
    (["failed"], "failed"),
    (["refused"], "refused"),
    (["outcome_unknown"], "outcome_unknown"),
    (["vetoed"], "vetoed"),
    (["escalated"], "escalated"),
    (["done", "done"], "done"),
    (["done", "already_posted"], "done"),
    (["already_posted", "already_posted"], "done"),
    (["done", "failed"], "partial"),
    (["done", "refused"], "partial"),
    (["done", "vetoed"], "partial"),
    (["done", "escalated"], "partial"),
    (["done", "outcome_unknown"], "partial"),
    (["already_posted", "failed"], "partial"),
    (["failed", "done"], "partial"),
    (["vetoed", "done"], "partial"),
    (["partial", "done"], "partial"),
    (["partial", "failed"], "partial"),
    (["failed", "failed"], "failed"),
    (["refused", "failed"], "refused"),
    (["failed", "refused"], "failed"),
    (["vetoed", "failed"], "vetoed"),
    (["outcome_unknown", "failed"], "outcome_unknown"),
    (["failed", "outcome_unknown"], "outcome_unknown"),
    (["refused", "outcome_unknown"], "outcome_unknown"),
    (["vetoed", "outcome_unknown"], "outcome_unknown"),
    (["outcome_unknown", "done"], "partial"),
    (["escalated", "vetoed"], "escalated"),
    (["done", "done", "failed"], "partial"),
    (["done", "done", "done"], "done"),
]


@pytest.mark.parametrize(("results", "expected"), AGGREGATION)
def test_record_result_aggregation_over_write_results(results: list[str], expected: str) -> None:
    assert aggregate_result(results) == expected


def test_aggregation_of_no_writes_or_an_unknown_result_is_an_error() -> None:
    with pytest.raises(ValueError):
        aggregate_result([])
    with pytest.raises(ValueError):
        aggregate_result(["attempted"])
    with pytest.raises(ValueError):
        aggregate_result(["done", "bogus"])


def test_every_write_result_combination_pair_is_schema_valid(dirs: files.StateDirs) -> None:
    exits = [
        lower(),
        lower(already_posted=True),
        lower(exit=20, code="refused", reason="stale_head", ids=(), already_posted=None),
        lower(exit=12, code="outcome_unknown", ids=(), already_posted=None),
        lower(exit=12, code="network_error", ids=(), already_posted=None),
    ]
    for item in exits:
        trail = AuditTrail.open(dirs)
        trail.attempted(run_record())
        trail.outcome(
            [WriteRecord.from_lower(0, TS, "comment", None, "k1", item)],
        )
        trail.close()
    for rec in read_records(dirs):
        validate("pr-comment/audit.json", rec)


# --- no bodies, hostile text -------------------------------------------------------------------


def test_no_record_contains_body_text_of_the_inputs(dirs: files.StateDirs) -> None:
    # The types hold hashes and counts only; this guards the whole path to the file.
    body = BODY_TEXT
    rec_inputs = inputs(
        bodies=({"role": "body", "sha256": SHA256, "bytes": len(body.encode())},),
    )
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record(inputs=rec_inputs))
    trail.outcome([done_write()])
    raw = audit_path(dirs).read_bytes()
    assert b"TOP-SECRET" not in raw
    assert b"script" not in raw


def test_untrusted_text_is_escaped_so_the_file_is_ascii_one_line_per_record(
    dirs: files.StateDirs,
) -> None:
    evil = 'main\n{"result":"done"}\r  \x00\x1b[31m‮𐏿\udc00é😀'
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record(inputs=inputs(base_ref=evil)))
    raw = audit_path(dirs).read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert b"\x1b" not in raw and b"\r" not in raw
    (rec,) = read_records(dirs)
    assert rec["inputs"]["base_ref"] == evil  # lossless, including the lone surrogates


def test_forgectl_text_in_lower_results_is_escaped_too(dirs: files.StateDirs) -> None:
    hostile = lower(ids=("gh:ic:x\n ",), reason=None)
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    trail.outcome([WriteRecord.from_lower(0, TS, "comment", None, "k1", hostile)])
    raw = audit_path(dirs).read_bytes()
    assert raw.count(b"\n") == 2
    raw.decode("ascii")


def test_nan_and_non_serializable_values_are_refused_not_written(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record(inputs=inputs(counts={"inline": float("nan")})))
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record(inputs=inputs(counts={"inline": object()})))
    assert audit_path(dirs).read_bytes() == b""


def test_a_deeply_nested_value_is_refused_not_a_crash(dirs: files.StateDirs) -> None:
    deep: Any = {}
    cursor = deep
    for _ in range(300000):
        cursor["x"] = {}
        cursor = cursor["x"]
    trail = AuditTrail.open(dirs)
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record(inputs=inputs(counts=deep)))
    assert audit_path(dirs).read_bytes() == b""


def test_a_huge_record_is_refused_before_the_attempted_write(dirs: files.StateDirs) -> None:
    bodies = tuple({"role": f"inline:f{i}", "sha256": SHA256, "bytes": 1} for i in range(20000))
    trail = AuditTrail.open(dirs)
    with pytest.raises(AuditUnavailableError) as info:
        trail.attempted(run_record(inputs=inputs(bodies=bodies)))
    assert info.value.details.get("too_large") is True
    assert audit_path(dirs).read_bytes() == b""


def test_construction_rejects_unknown_values() -> None:
    with pytest.raises(ValueError):
        run_record(action="delete")
    with pytest.raises(ValueError):
        run_record(decision_ids=("bad id",))
    with pytest.raises(ValueError):
        run_record(decision_ids=tuple(f"d{i}" for i in range(33)))
    with pytest.raises(ValueError):
        inputs(override_escalation=("everything",))
    with pytest.raises(ValueError):
        inputs(event="reject")
    with pytest.raises(ValueError):
        WriteRecord.blocked(0, TS, "comment", None, "k", "done")
    with pytest.raises(ValueError):
        WriteRecord(0, TS, "comment", None, "k", "vetoed", lower())
    with pytest.raises(ValueError):
        WriteRecord(0, TS, "comment", None, "k", "done", None)


# --- unavailable and failed --------------------------------------------------------------------


def test_open_on_an_unwritable_audit_file_is_audit_unavailable(dirs: files.StateDirs) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    audit_path(dirs).write_bytes(b"")
    os.chmod(audit_path(dirs), 0o400)
    with pytest.raises(AuditUnavailableError) as info:
        AuditTrail.open(dirs)
    assert info.value.details["path"] == str(audit_path(dirs))
    assert info.value.details["errno"] == "EACCES"


def test_open_on_a_symlinked_audit_file_is_audit_unavailable_and_not_followed(
    dirs: files.StateDirs, tmp_path: Path
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep\n")
    audit_path(dirs).symlink_to(victim)
    with pytest.raises(AuditUnavailableError):
        AuditTrail.open(dirs)
    assert victim.read_bytes() == b"keep\n"


def test_open_on_a_directory_is_audit_unavailable(dirs: files.StateDirs) -> None:
    audit_path(dirs).mkdir()
    with pytest.raises(AuditUnavailableError):
        AuditTrail.open(dirs)


def test_the_unavailable_error_builds_the_exit_12_envelope() -> None:
    err = AuditUnavailableError("cannot append", {"path": "/x\ny", "errno": "ENOSPC"})
    error = audit.audit_unavailable_error(err)["error"]
    assert error["code"] == "audit_unavailable"
    assert (error["landed"], error["retry"]) == (False, "same_command")
    assert error["details"] == {"path": "/x\ny", "errno": "ENOSPC"}
    assert error["message"] == "cannot append"
    assert errors.exit_code_for("audit_unavailable") == 12


def test_a_swapped_audit_file_before_attempted_is_audit_unavailable(
    dirs: files.StateDirs, tmp_path: Path
) -> None:
    trail = AuditTrail.open(dirs)
    victim = tmp_path / "victim"
    victim.write_bytes(b"")
    audit_path(dirs).unlink()
    audit_path(dirs).symlink_to(victim)
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record())
    assert victim.read_bytes() == b""


def proxy(**overrides: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{**vars(os), **overrides})


def test_a_full_disk_on_attempted_is_audit_unavailable_and_retryable(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(appendfile, "os", proxy(write=full))
    with pytest.raises(AuditUnavailableError) as info:
        trail.attempted(run_record())
    assert info.value.details["errno"] == "ENOSPC"
    monkeypatch.undo()
    trail.attempted(run_record())  # nothing was recorded: it may be tried again
    assert len(read_records(dirs)) == 1


def test_a_failed_fsync_on_attempted_is_audit_unavailable(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)

    def bad(fd: int) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(appendfile, "os", proxy(fsync=bad))
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record())


def test_both_records_are_fsynced(dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(appendfile, "os", proxy(fsync=calls.append))
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    trail.outcome([done_write()])
    assert len(calls) == 2


def test_a_full_disk_on_the_outcome_is_audit_failed_with_the_writes_pair(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(appendfile, "os", proxy(write=full))
    with pytest.raises(AuditFailedError) as info:
        trail.outcome([done_write()])
    exc = info.value
    assert exc.result == "done"
    assert exc.details["errno"] == "ENOSPC"
    assert exc.write_pair == (True, "none")


@pytest.mark.parametrize(
    ("kwargs", "pair"),
    [
        ({}, (True, "none")),
        ({"already_posted": True}, (True, "none")),
        ({"exit": 12, "code": "outcome_partial"}, (True, "none")),
        ({"exit": 12, "code": "outcome_unknown"}, (None, "same_command")),
        ({"exit": 12, "code": "network_error"}, (False, "same_command")),
        ({"exit": 20, "code": "stale_head"}, (False, "same_command")),
    ],
)
def test_audit_failed_pair_follows_the_write_not_a_blanket_retry(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], pair: Any
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    monkeypatch.setattr(
        appendfile,
        "os",
        proxy(write=lambda fd, data: (_ for _ in ()).throw(OSError(errno.EIO, "io"))),
    )
    with pytest.raises(AuditFailedError) as info:
        trail.outcome([done_write(lower=lower(**kwargs))])
    assert info.value.write_pair == pair


def test_a_landed_write_plus_a_failed_one_gives_a_landed_pair_on_audit_failed(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    monkeypatch.setattr(
        appendfile,
        "os",
        proxy(write=lambda fd, data: (_ for _ in ()).throw(OSError(errno.EIO, "io"))),
    )
    failed = lower(exit=12, code="network_error", ids=(), already_posted=None)
    with pytest.raises(AuditFailedError) as info:
        trail.outcome([done_write(0), done_write(1, lower=failed)])
    assert info.value.result == "partial"
    assert info.value.write_pair == (True, "none")


def test_audit_failed_error_envelope_carries_the_result_document(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    monkeypatch.setattr(
        appendfile,
        "os",
        proxy(write=lambda fd, data: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))),
    )
    with pytest.raises(AuditFailedError) as info:
        trail.outcome([done_write()])
    document = {"pr": "example.test/o/r#7", "result": "done"}
    envelope = audit_failed_error(info.value, document)["error"]
    assert envelope["code"] == "audit_failed"
    assert envelope["details"]["outcome"] == document
    assert (envelope["landed"], envelope["retry"]) == (True, "none")
    assert "\n" not in envelope["message"]


def test_the_outcome_may_be_retried_after_a_failed_append(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    monkeypatch.setattr(
        appendfile,
        "os",
        proxy(write=lambda fd, data: (_ for _ in ()).throw(OSError(errno.EIO, "io"))),
    )
    with pytest.raises(AuditFailedError):
        trail.outcome([done_write()])
    monkeypatch.undo()
    assert trail.outcome([done_write()]) == "done"
    assert [r["result"] for r in read_records(dirs)] == ["attempted", "done"]


def test_a_torn_outcome_leaves_a_skippable_line_and_the_next_run_starts_clean(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    real = os.write
    state = {"n": 0}

    def torn(fd: int, data: bytes) -> int:
        state["n"] += 1
        if state["n"] == 1:
            return real(fd, bytes(data)[:10])
        if state["n"] == 2:
            raise OSError(errno.ENOSPC, "full")
        return real(fd, data)

    monkeypatch.setattr(appendfile, "os", proxy(write=torn))
    with pytest.raises(AuditFailedError):
        trail.outcome([done_write()])
    monkeypatch.undo()
    full_run(dirs)
    lines = audit_path(dirs).read_bytes().splitlines()
    good: list[dict[str, Any]] = []
    for line in lines:
        try:
            good.append(json.loads(line))
        except ValueError:
            pass
    assert [r["result"] for r in good] == ["attempted", "attempted", "done"]


def test_a_stale_clock_is_not_used_the_timestamps_are_rfc3339_utc(dirs: files.StateDirs) -> None:
    full_run(dirs)
    for rec in read_records(dirs):
        assert rec["ts"].endswith("Z")
        datetime.fromisoformat(rec["ts"].replace("Z", "+00:00")).astimezone(UTC)


def test_the_clock_is_injectable(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs, clock=lambda: datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=UTC))
    trail.attempted(run_record())
    (rec,) = read_records(dirs)
    assert rec["ts"] == "2030-01-02T03:04:05.678Z"


def test_module_does_not_import_jsonschema() -> None:
    assert "jsonschema" not in vars(audit)


def test_a_repeated_attempted_after_a_failed_fsync_does_not_append_again(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)

    def bad(fd: int) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(appendfile, "os", proxy(fsync=bad))
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record())
    monkeypatch.undo()
    with pytest.raises(AuditUnavailableError):
        trail.attempted(run_record())
    assert len(read_records(dirs)) == 1


def test_a_repeated_outcome_after_a_failed_fsync_does_not_append_again(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())

    def bad(fd: int) -> None:
        raise OSError(errno.EIO, "io")

    monkeypatch.setattr(appendfile, "os", proxy(fsync=bad))
    with pytest.raises(AuditFailedError) as first:
        trail.outcome([done_write()])
    monkeypatch.undo()
    with pytest.raises(AuditFailedError) as second:
        trail.outcome([done_write()])
    assert second.value.write_pair == first.value.write_pair == (True, "none")
    assert [r["result"] for r in read_records(dirs)] == ["attempted", "done"]


def test_a_repeated_outcome_after_a_partial_write_does_not_append_again(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    real = os.write
    state = {"n": 0}

    def torn(fd: int, data: bytes) -> int:
        state["n"] += 1
        if state["n"] == 1:
            return real(fd, bytes(data)[:10])
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(appendfile, "os", proxy(write=torn))
    with pytest.raises(AuditFailedError):
        trail.outcome([done_write()])
    monkeypatch.undo()
    with pytest.raises(AuditFailedError):
        trail.outcome([done_write()])
    assert audit_path(dirs).read_bytes().count(b'"result":"done"') == 0


def leftover_lower(**overrides: Any) -> LowerResult:
    return lower(exit=12, code="host_rejected", ids=(), leftover_drafts=True, **overrides)


@pytest.mark.parametrize(
    ("writes", "pair"),
    [
        ([leftover_lower()], (False, "none")),
        ([lower(exit=12, code="host_rejected", ids=())], (False, "same_command")),
        ([lower(), leftover_lower()], (True, "none")),
        ([leftover_lower(), lower(exit=12, code="outcome_unknown", ids=())], (False, "none")),
        ([lower(exit=12, code="outcome_unknown", ids=())], (None, "same_command")),
        (
            [lower(exit=12, code="network_error", ids=()), lower(exit=12, code="outcome_unknown")],
            (None, "same_command"),
        ),
        ([lower(exit=12, code="outcome_partial")], (True, "none")),
    ],
)
def test_audit_failed_pair_with_leftover_drafts_and_unknown_outcomes(
    dirs: files.StateDirs,
    monkeypatch: pytest.MonkeyPatch,
    writes: list[LowerResult],
    pair: Any,
) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    monkeypatch.setattr(
        appendfile,
        "os",
        proxy(write=lambda fd, data: (_ for _ in ()).throw(OSError(errno.EIO, "io"))),
    )
    records = [done_write(i, lower=item) for i, item in enumerate(writes)]
    with pytest.raises(AuditFailedError) as info:
        trail.outcome(records)
    assert info.value.write_pair == pair


def test_leftover_drafts_is_not_written_to_the_record(dirs: files.StateDirs) -> None:
    trail = AuditTrail.open(dirs)
    trail.attempted(run_record())
    trail.outcome([done_write(lower=leftover_lower())])
    assert "leftover_drafts" not in read_records(dirs)[1]["writes"][0]["lower"]
