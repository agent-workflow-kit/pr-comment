"""``pr-comment export`` (issue #38): filters, pairing, hostile files, streaming, goldens."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from pr_comment import cli, export, files
from tests.schema_harness import load_schemas, validate

SHA = "a" * 40
SHA256 = "b" * 64
ATT1 = "abcDEF123_-xyz0000001"
ATT2 = "abcDEF123_-xyz0000002"
ATT3 = "abcDEF123_-xyz0000003"
T0 = "2026-11-10T10:00:00.000Z"
T1 = "2026-11-10T10:00:05.000Z"
T2 = "2026-11-12T08:30:00.000Z"
T3 = "2026-11-12T08:30:09.000Z"
T4 = "2026-11-16T23:59:59.999Z"


def zero_counts() -> dict[str, Any]:
    def zero(schema: dict[str, Any]) -> Any:
        if schema.get("type") == "object":
            return {k: zero(v) for k, v in schema["properties"].items()}
        return 0

    return zero(load_schemas()["pr-comment/defs.json"]["$defs"]["counts"])


def audit_rec(
    attempt_id: str, result: str = "attempted", ts: str = T0, **over: Any
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "ts": ts,
        "tool": "pr-comment",
        "version": "0.1.0",
        "attempt_id": attempt_id,
        "actor": "auto:bot",
        "actor_source": "flag",
        "stdin_tty": False,
        "decision_id": [],
        "override": [],
        "action": "comment",
        "target": "example.com/acme/repo#7",
        "pr_id": "gh:pr:abc123",
        "inputs": {
            "event": None,
            "draft_id": None,
            "draft_sha256": None,
            "anchor_head": None,
            "reanchor_to": None,
            "pinned_head": SHA,
            "base_ref": "main",
            "dedupe_key": "k1",
            "to": None,
            "threads": [],
            "bodies": [{"role": "body", "sha256": SHA256, "bytes": 12}],
            "counts": zero_counts(),
            "plan_sha256": SHA256,
            "allowed_mentions": [],
            "override_escalation": [],
            "config_hash": SHA256,
            "config_path": None,
        },
        "result": result,
    }
    if result != "attempted":
        rec["hook_decision_id"] = []
        rec["writes"] = [
            {
                "index": 0,
                "ts": ts,
                "action": "comment",
                "thread_id": None,
                "dedupe_key": "k1",
                "result": "done",
                "lower": {
                    "exit": 0,
                    "code": None,
                    "reason": None,
                    "ids": ["gh:ic:x1"],
                    "thread_ids": [],
                    "review_id": None,
                    "already_posted": False,
                },
            }
        ]
    rec.update(over)
    return rec


def log_rec(event: str = "refused", ts: str = T0, **over: Any) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "ts": ts,
        "level": "warn",
        "event": event,
        "pr": "example.com/acme/repo#7",
        "attempt_id": None,
        "details": {"why": "x"},
    }
    rec.update(over)
    return rec


def line(rec: dict[str, Any]) -> bytes:
    return json.dumps(rec, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"


def state_dir() -> Path:
    return files.state_dir_path()


def put(name: str, *chunks: bytes | dict[str, Any], mode: int = 0o600) -> Path:
    """Write ``audit.jsonl`` or ``log.jsonl`` in the (private) state directory."""
    with files.open_state_dirs():
        pass
    path = state_dir() / name
    path.write_bytes(b"".join(c if isinstance(c, bytes) else line(c) for c in chunks))
    path.chmod(mode)
    return path


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(["export", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def out_lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(text) for text in out.splitlines()]


# --- output shapes -------------------------------------------------------------------------


def test_a_missing_file_prints_nothing_or_an_empty_array(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(capsys) == (0, "", "")
    assert run(capsys, "--log") == (0, "", "")
    assert run(capsys, "--json") == (0, "[]\n", "")
    assert run(capsys, "--json", "--log") == (0, "[]\n", "")


def test_an_empty_file_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl")
    assert run(capsys) == (0, "", "")
    assert run(capsys, "--json") == (0, "[]\n", "")


def test_audit_records_are_printed_as_jsonl_in_file_order(
    capsys: pytest.CaptureFixture[str],
) -> None:
    records = [audit_rec(ATT1, "attempted", T0), audit_rec(ATT1, "done", T1)]
    put("audit.jsonl", *records)
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert out_lines(out) == records
    assert out.count("\n") == len(records)


def test_json_prints_one_array_that_validates_and_matches_the_golden(
    capsys: pytest.CaptureFixture[str], golden_json: Callable[[str, Any], None]
) -> None:
    records = [
        audit_rec(ATT1, "attempted", T0),
        audit_rec(ATT1, "done", T1),
        audit_rec(ATT2, "attempted", T2),
        audit_rec(ATT2, "outcome_unknown", T3),
    ]
    put("audit.jsonl", *records)
    code, out, err = run(capsys, "--json")
    assert (code, err) == (0, "")
    document = json.loads(out)
    validate("pr-comment/cli/export.json", document)
    assert document == records
    golden_json("cli/export-audit.json", document)


def test_log_records_come_from_log_jsonl_with_log_flag(
    capsys: pytest.CaptureFixture[str], golden_json: Callable[[str, Any], None]
) -> None:
    records = [
        log_rec("usage_error", T0, level="info", pr=None),
        log_rec("refused", T1, attempt_id=ATT1, details=None),
        log_rec("config_warning", T2, details={"file": "config.yaml", "keys": ["mentions"]}),
    ]
    put("audit.jsonl", audit_rec(ATT1))
    put("log.jsonl", *records)
    code, out, err = run(capsys, "--log", "--json")
    assert (code, err) == (0, "")
    document = json.loads(out)
    validate("pr-comment/cli/export.json", document)
    assert document == records
    golden_json("cli/export-log.json", document)
    code, out, _ = run(capsys, "--log")
    assert out_lines(out) == records


def test_the_default_reads_the_audit_file_not_the_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put("audit.jsonl", audit_rec(ATT1))
    put("log.jsonl", log_rec())
    _, out, _ = run(capsys)
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT1]


def test_export_never_writes_or_creates_a_file(capsys: pytest.CaptureFixture[str]) -> None:
    run(capsys)
    assert not (state_dir() / "audit.jsonl").exists()
    assert not (state_dir() / "log.jsonl").exists()
    path = put("audit.jsonl", audit_rec(ATT1))
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    run(capsys, "--json")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


# --- --since ---------------------------------------------------------------------------------


def test_since_with_a_timestamp_keeps_records_at_or_after_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put(
        "audit.jsonl",
        audit_rec(ATT1, "done", T1),
        audit_rec(ATT2, "done", T2),
        audit_rec(ATT3, "done", T4),
    )
    _, out, _ = run(capsys, "--since", T2)
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT2, ATT3]
    _, out, _ = run(capsys, "--since", "2026-11-12T10:30:00+02:00")  # 08:30:00Z: boundary included
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT2, ATT3]
    _, out, _ = run(capsys, "--since", "2026-11-12T08:30:00.001Z")
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT3]


def test_since_compares_instants_not_text(capsys: pytest.CaptureFixture[str]) -> None:
    # 23:00-05:00 on the 11th is 04:00Z on the 12th: after 03:00Z, though "11" < "12" as text.
    put("audit.jsonl", audit_rec(ATT1, "done", "2026-11-11T23:00:00-05:00"))
    _, out, _ = run(capsys, "--since", "2026-11-12T03:00:00Z")
    assert len(out_lines(out)) == 1
    _, out, _ = run(capsys, "--since", "2026-11-12T05:00:00Z")
    assert out == ""


def test_since_with_a_duration_counts_back_from_now(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 11, 17, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(export, "now_utc", lambda: now)

    def stamp(delta: timedelta) -> str:
        return (now - delta).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    put(
        "audit.jsonl",
        audit_rec(ATT1, "done", stamp(timedelta(days=8))),
        audit_rec(ATT2, "done", stamp(timedelta(days=6, hours=23))),
        audit_rec(ATT3, "done", stamp(timedelta(minutes=1))),
    )
    _, out, _ = run(capsys, "--since", "7d")
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT2, ATT3]
    _, out, _ = run(capsys, "--since", "90s")
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT3]
    _, out, _ = run(capsys, "--since", "3650d")
    assert len(out_lines(out)) == 3


def test_since_filters_the_log_too(capsys: pytest.CaptureFixture[str]) -> None:
    put("log.jsonl", log_rec(ts=T0), log_rec(ts=T4))
    _, out, _ = run(capsys, "--log", "--since", T2)
    assert [r["ts"] for r in out_lines(out)] == [T4]


def test_a_since_in_the_future_selects_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl", audit_rec(ATT1, "done", T4))
    assert run(capsys, "--since", "2999-01-01T00:00:00Z") == (0, "", "")
    assert run(capsys, "--json", "--since", "2999-01-01T00:00:00Z") == (0, "[]\n", "")


@pytest.mark.parametrize(
    "value", ["", "7", "0d", "-1d", "7x", "2026-11-12", "2026-11-12T08:30:00", "yesterday", "7d\n"]
)
def test_an_invalid_since_is_exit_2_usage_naming_the_flag_and_reads_nothing(
    capsys: pytest.CaptureFixture[str], value: str
) -> None:
    code, out, _ = run(capsys, "--json", "--since", value)
    document = json.loads(out)
    assert code == 2
    assert document["error"]["code"] == "usage"
    assert "--since" in document["error"]["message"]
    validate("pr-comment/cli/error.json", document)
    assert not state_dir().exists()


def test_since_given_twice_with_different_values_is_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run(capsys, "--since", "7d", "--since", "8d")[0] == 2


# --- malformed lines -------------------------------------------------------------------------


def test_a_malformed_line_is_skipped_with_a_warning_and_the_rest_is_printed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good1, good2 = audit_rec(ATT1, "attempted", T0), audit_rec(ATT2, "done", T2)
    put("audit.jsonl", good1, b"this is not json\n", good2)
    code, out, err = run(capsys)
    assert code == 0
    assert out_lines(out) == [good1, good2]
    assert "audit.jsonl" in err
    assert "line 2" in err
    assert "not json" not in err  # never echoes the content


def test_a_torn_last_line_without_a_newline_is_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = audit_rec(ATT1)
    put("audit.jsonl", good, line(audit_rec(ATT2))[:-1])
    code, out, err = run(capsys)
    assert code == 0
    assert out_lines(out) == [good]
    assert "line 2" in err


def test_a_complete_record_missing_only_its_newline_is_still_torn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put("audit.jsonl", audit_rec(ATT1), line(audit_rec(ATT2))[:-1])
    assert len(out_lines(run(capsys)[1])) == 1


def test_a_torn_line_ended_by_a_newline_loses_only_that_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put("audit.jsonl", audit_rec(ATT1), line(audit_rec(ATT2))[:50] + b"\n", audit_rec(ATT3))
    code, out, err = run(capsys)
    assert code == 0
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT1, ATT3]
    assert "line 2" in err


def test_empty_lines_are_skipped_without_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl", b"\n", audit_rec(ATT1), b"\n\n", audit_rec(ATT2))
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert len(out_lines(out)) == 2


HOSTILE_LINES = {
    "invalid utf-8": b'{"ts":"\xff"}\n',
    "non-ascii utf-8": '{"ts":"é"}\n'.encode(),
    "raw escape byte": b'{"a":"\x1b[2J"}\n',
    "raw tab in string": b'{"a":"x\ty"}\n',
    "nul byte": b"\x00\x00\n",
    "a json array": b"[1,2,3]\n",
    "a json string": b'"text"\n',
    "a number": b"7\n",
    "null": b"null\n",
    "nan": b'{"ts":NaN}\n',
    "infinity": b'{"x":Infinity}\n',
    "duplicate keys": b'{"result":"done","result":"attempted"}\n',
    "deeply nested": b"[" * 100000 + b"]" * 100000 + b"\n",
    "huge integer": b'{"x":' + b"9" * 20000 + b"}\n",
    "crlf only": b"\r\n",
    "bom": b"\xef\xbb\xbf{}\n",
    "empty object": b"{}\n",
}


@pytest.mark.parametrize("content", HOSTILE_LINES.values(), ids=HOSTILE_LINES.keys())
def test_hostile_lines_are_skipped_and_never_break_the_run(
    capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    good = audit_rec(ATT1)
    put("audit.jsonl", content, good)
    code, out, err = run(capsys)
    assert code == 0
    assert out_lines(out) == [good]
    for text in (out, err):
        assert text.isascii()
        assert not any(ord(c) < 32 and c != "\n" for c in text)


def test_records_that_break_the_audit_schema_basics_are_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = audit_rec(ATT1)
    bad = [
        audit_rec(ATT2, ts="yesterday"),
        audit_rec(ATT2, ts="2026-11-12T08:30:00"),  # no offset
        {**audit_rec(ATT2), "ts": 12},
        audit_rec(ATT2, tool="other"),
        {**audit_rec(ATT2), "attempt_id": "short"},
        {**audit_rec(ATT2), "attempt_id": 7},
        audit_rec(ATT2, result="bogus"),
        {**audit_rec(ATT2, "done"), "result": ["done"]},
        audit_rec(ATT2, "done", writes="x"),
        audit_rec(ATT2, inputs=[]),
    ]
    missing = audit_rec(ATT2)
    del missing["actor"]
    put("audit.jsonl", *bad, missing, good)
    code, out, err = run(capsys)
    assert code == 0
    assert out_lines(out) == [good]
    assert err.count("skipped") >= 1


def test_records_that_break_the_log_schema_basics_are_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = log_rec()
    bad = [
        log_rec(ts="x"),
        log_rec(level="loud"),
        log_rec(event="bogus"),
        log_rec(attempt_id="x"),
        log_rec(pr=3),
        {k: v for k, v in log_rec().items() if k != "details"},
        log_rec(details=[1]),
        audit_rec(ATT1),  # an audit record in the log file
    ]
    put("log.jsonl", *bad, good)
    code, out, _ = run(capsys, "--log")
    assert code == 0
    assert out_lines(out) == [good]


def test_a_log_record_in_the_audit_file_and_the_reverse_are_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put("audit.jsonl", log_rec(), audit_rec(ATT1))
    assert [r["attempt_id"] for r in out_lines(run(capsys)[1])] == [ATT1]


def test_an_audit_line_over_one_mib_is_skipped_without_reading_it_whole(
    capsys: pytest.CaptureFixture[str],
) -> None:
    good = audit_rec(ATT1)
    huge = line(audit_rec(ATT2, details_pad="x" * (1 << 20)))
    assert len(huge) > 1 << 20
    put("audit.jsonl", good, huge, audit_rec(ATT3))
    code, out, err = run(capsys)
    assert code == 0
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT1, ATT3]
    assert "line 2" in err


def test_a_log_line_over_64_kib_is_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    big = line(log_rec(details={"pad": "x" * (70 * 1024)}))
    put("log.jsonl", log_rec(ts=T0), big, log_rec(ts=T1))
    code, out, err = run(capsys, "--log")
    assert code == 0
    assert [r["ts"] for r in out_lines(out)] == [T0, T1]
    assert "line 2" in err


def test_a_file_with_one_endless_line_is_bounded(capsys: pytest.CaptureFixture[str]) -> None:
    put("log.jsonl", b"x" * (5 << 20) + b"\n", log_rec())
    tracemalloc.start()
    try:
        code, out, _ = run(capsys, "--log")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert code == 0
    assert out_lines(out) == [log_rec()]
    assert peak < 3 << 20  # far below the 5 MiB line


def test_warnings_are_capped_and_summarized(capsys: pytest.CaptureFixture[str]) -> None:
    put("log.jsonl", *([b"junk\n"] * 100), log_rec())
    code, out, err = run(capsys, "--log")
    assert code == 0
    assert len(out_lines(out)) == 1
    assert err.count("\n") <= export.MAX_WARNINGS + 1
    assert "80 more" in err


def test_quiet_drops_the_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl", b"junk\n", audit_rec(ATT1))
    code, out, err = run(capsys, "--quiet")
    assert (code, err) == (0, "")
    assert len(out_lines(out)) == 1


# --- pairing ---------------------------------------------------------------------------------


def test_the_last_record_per_attempt_id_and_kind_is_authoritative(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = audit_rec(ATT1, "attempted", T0, actor="auto:first")
    outcome = audit_rec(ATT1, "outcome_unknown", T1)
    other = audit_rec(ATT2, "attempted", T2)
    repeat = audit_rec(ATT1, "attempted", T3, actor="auto:last")
    final = audit_rec(ATT1, "done", T4)
    put("audit.jsonl", first, outcome, other, repeat, final)
    code, out, _ = run(capsys)
    assert code == 0
    # Each survivor stays where its last copy was written.
    assert out_lines(out) == [other, repeat, final]


def test_unmatched_records_are_kept(capsys: pytest.CaptureFixture[str]) -> None:
    only_attempted = audit_rec(ATT1, "attempted", T0)
    only_outcome = audit_rec(ATT2, "refused", T1)
    put("audit.jsonl", only_attempted, only_outcome)
    assert out_lines(run(capsys)[1]) == [only_attempted, only_outcome]


def test_records_with_the_same_id_but_different_kinds_are_not_duplicates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put(
        "audit.jsonl",
        audit_rec(ATT1, "attempted", T0),
        audit_rec(ATT1, "partial", T1),
        audit_rec(ATT1, "failed", T2),  # a second outcome for the id replaces the first
    )
    assert [r["result"] for r in out_lines(run(capsys)[1])] == ["attempted", "failed"]


def test_the_authoritative_record_decides_the_since_filter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The first copy is inside the window, the last one (authoritative) is outside it.
    put("audit.jsonl", audit_rec(ATT1, "attempted", T4), audit_rec(ATT1, "attempted", T0))
    assert run(capsys, "--since", T2)[1] == ""
    # The reverse: only the last copy is inside.
    put("audit.jsonl", audit_rec(ATT1, "attempted", T0), audit_rec(ATT1, "attempted", T4))
    assert len(out_lines(run(capsys, "--since", T2)[1])) == 1


def test_log_records_are_never_deduplicated(capsys: pytest.CaptureFixture[str]) -> None:
    rec = log_rec(attempt_id=ATT1)
    put("log.jsonl", rec, rec, rec)
    assert len(out_lines(run(capsys, "--log")[1])) == 3


def test_too_many_distinct_records_is_audit_unavailable_not_unbounded_memory(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(export, "MAX_TRACKED", 3)
    ids = [f"abcdefgh{n:04d}" for n in range(4)]
    put("audit.jsonl", *[audit_rec(i) for i in ids])
    code, out, _ = run(capsys, "--json")
    assert code == 12
    assert json.loads(out)["error"]["code"] == "audit_unavailable"
    assert "--since" in json.loads(out)["error"]["message"]
    put("audit.jsonl", *[audit_rec(i) for i in ids[:3]])
    assert run(capsys)[0] == 0


# --- terminal safety ---------------------------------------------------------------------------


def test_output_is_ascii_even_for_a_record_that_holds_control_and_bidi_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile = "‮\u001b[2J\u0007\u009b  rm -rf \u007f end \U0001f600"
    rec = log_rec(details={"text": hostile})
    # A hand-written file may hold a raw DEL byte (valid ASCII, legal inside a JSON string).
    raw = line(rec).replace(b"\\u007f", b"\x7f")
    assert b"\x7f" in raw
    put("log.jsonl", raw)
    code, out, err = run(capsys, "--log")
    assert (code, err) == (0, "")
    assert out.isascii()
    assert all(c == "\n" or 32 <= ord(c) < 127 for c in out)
    assert out_lines(out)[0]["details"]["text"] == hostile
    code, out, _ = run(capsys, "--log", "--json")
    assert all(c == "\n" or 32 <= ord(c) < 127 for c in out)


def test_json_mode_output_equals_the_jsonl_records(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl", audit_rec(ATT1), audit_rec(ATT2, "done", T2))
    assert json.loads(run(capsys, "--json")[1]) == out_lines(run(capsys)[1])


# --- unsafe files and directories --------------------------------------------------------------


def error_of(out: str) -> dict[str, Any]:
    document = json.loads(out)
    validate("pr-comment/cli/error.json", document)
    return document["error"]


def test_a_symlink_is_refused_and_not_followed(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_bytes(line(audit_rec(ATT1)))
    with files.open_state_dirs():
        pass
    (state_dir() / "audit.jsonl").symlink_to(target)
    code, out, err = run(capsys, "--json")
    assert code == 12
    assert error_of(out)["code"] == "audit_unavailable"
    assert error_of(out)["details"]["path"].endswith("audit.jsonl")
    assert "ELOOP" in json.dumps(error_of(out)["details"]) or "symlink" in error_of(out)["message"]
    assert "pr-comment:" in err


def test_a_dangling_symlink_is_refused_not_treated_as_missing(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    with files.open_state_dirs():
        pass
    (state_dir() / "log.jsonl").symlink_to(tmp_path / "nowhere")
    code, out, _ = run(capsys, "--json", "--log")
    assert code == 12
    assert error_of(out)["code"] == "audit_unavailable"


def test_a_fifo_is_refused_without_blocking(capsys: pytest.CaptureFixture[str]) -> None:
    with files.open_state_dirs():
        pass
    os.mkfifo(state_dir() / "audit.jsonl", 0o600)
    code, out, _ = run(capsys, "--json")
    assert code == 12
    assert error_of(out)["code"] == "audit_unavailable"


def test_a_directory_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with files.open_state_dirs():
        pass
    (state_dir() / "audit.jsonl").mkdir()
    assert run(capsys, "--json")[0] == 12


def test_a_file_with_two_hard_links_is_refused(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = put("audit.jsonl", audit_rec(ATT1))
    os.link(path, tmp_path / "second-name")
    code, out, _ = run(capsys, "--json")
    assert code == 12
    assert error_of(out)["code"] == "audit_unavailable"


@pytest.mark.parametrize("mode", [0o666, 0o620, 0o602])
def test_a_file_writable_by_group_or_others_is_refused(
    capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    put("audit.jsonl", audit_rec(ATT1), mode=mode)
    assert run(capsys, "--json")[0] == 12


def test_a_file_readable_by_others_but_not_writable_is_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    put("audit.jsonl", audit_rec(ATT1), mode=0o644)
    assert run(capsys)[0] == 0


def test_an_unreadable_file_is_audit_unavailable(capsys: pytest.CaptureFixture[str]) -> None:
    path = put("audit.jsonl", audit_rec(ATT1), mode=0o200)
    if os.access(path, os.R_OK):
        pytest.skip("running as a user that ignores file modes")
    code, out, _ = run(capsys, "--json")
    assert code == 12
    assert error_of(out)["code"] == "audit_unavailable"


def test_a_file_owned_by_someone_else_is_refused(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("audit.jsonl", audit_rec(ATT1))
    real = os.fstat

    def foreign(fd: int) -> os.stat_result:
        st = real(fd)
        if stat.S_ISREG(st.st_mode):
            fields = list(st)
            fields[4] = st.st_uid + 1  # st_uid is the fifth field of the tuple form
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(os, "fstat", foreign)
    assert run(capsys, "--json")[0] == 12


def test_an_unsafe_state_directory_is_state_unsafe(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl", audit_rec(ATT1))
    state_dir().chmod(0o755)
    code, out, _ = run(capsys, "--json")
    assert code == 12
    assert error_of(out)["code"] == "state_unsafe"
    state_dir().chmod(0o700)


def test_a_symlinked_state_directory_is_state_unsafe(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    real = tmp_path / "real-state"
    real.mkdir(mode=0o700)
    base = state_dir().parent
    base.mkdir(parents=True)
    state_dir().symlink_to(real)
    code, out, _ = run(capsys, "--json")
    assert code == 12
    assert error_of(out)["code"] == "state_unsafe"


# --- streaming ---------------------------------------------------------------------------------


def _namespace(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {"since": None, "log": False, "json": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_a_large_log_streams_in_bounded_memory() -> None:
    record = line(log_rec(details={"pad": "y" * 80}))
    count = 15_000
    put("log.jsonl", record * count)
    outcome = export.handle_export(_namespace(log=True))
    assert outcome.exit_code == 0 and outcome.stream is not None
    seen = 0

    def sink(text: str) -> None:
        nonlocal seen
        seen += text.count("\n")

    tracemalloc.start()
    try:
        outcome.stream(sink)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert seen == count
    assert peak < 2 << 20


def test_a_large_audit_file_keeps_only_a_small_index() -> None:
    count = 5_000
    chunks = [line(audit_rec(f"abcdefgh{n:012d}")) for n in range(count)]
    put("audit.jsonl", *chunks)
    tracemalloc.start()
    try:
        outcome = export.handle_export(_namespace())
        assert outcome.stream is not None
        seen = 0

        def sink(text: str) -> None:
            nonlocal seen
            seen += text.count("\n")

        outcome.stream(sink)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert seen == count
    assert peak < 6 << 20


def test_a_reader_that_closes_the_pipe_ends_the_run_quietly(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("log.jsonl", *[log_rec(ts=T0)] * 10)
    real = cli._write  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def closing(stream: Any, text: str) -> None:
        nonlocal calls
        if text.startswith("{"):
            calls += 1
            raise BrokenPipeError
        real(stream, text)

    monkeypatch.setattr(cli, "_write", closing)
    assert cli.main(["export", "--log"]) == 0
    assert calls == 1
    assert capsys.readouterr().err == ""


def test_a_failure_while_streaming_is_an_internal_error_on_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("log.jsonl", log_rec())

    def broken(*_: Any) -> Any:
        raise OSError(5, "Input/output error: /secret/path")

    monkeypatch.setattr(export, "_emit_record", broken)
    code, out, err = run(capsys, "--log")
    assert code == 1
    assert "unexpected OSError" in err
    assert out == ""


def test_the_file_descriptor_is_closed_after_the_run(capsys: pytest.CaptureFixture[str]) -> None:
    put("audit.jsonl", audit_rec(ATT1))
    before = set(os.listdir("/proc/self/fd"))
    for _ in range(5):
        run(capsys, "--json")
        run(capsys)
    assert set(os.listdir("/proc/self/fd")) == before


def test_a_file_that_grows_during_the_read_does_not_lengthen_the_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = put("audit.jsonl", audit_rec(ATT1))
    outcome = export.handle_export(_namespace())
    with path.open("ab") as handle:
        handle.write(line(audit_rec(ATT2)))
    assert outcome.stream is not None
    pieces: list[str] = []
    outcome.stream(pieces.append)
    assert len(out_lines("".join(pieces))) == 1


# --- review round 1 ----------------------------------------------------------------------------


@pytest.mark.parametrize("huge", ["1e999", "1e400", "-1e400"])
@pytest.mark.parametrize("name", ["audit.jsonl", "log.jsonl"])
def test_a_float_that_overflows_is_skipped_with_a_warning(
    capsys: pytest.CaptureFixture[str], name: str, huge: str
) -> None:
    if name == "audit.jsonl":
        good1, good2 = audit_rec(ATT1), audit_rec(ATT2)
        bad = line(audit_rec(ATT3)).replace(
            b'"draft_sha256":null', b'"draft_sha256":' + huge.encode()
        )
    else:
        good1, good2 = log_rec(), log_rec()
        bad = line(log_rec()).replace(
            b'"details":{"why":"x"}', b'"details":{"why":' + huge.encode() + b"}"
        )
    assert huge.encode() in bad
    put(name, good1, bad, good2)
    args = [] if name == "audit.jsonl" else ["--log"]
    code, out, err = run(capsys, *args)
    assert code == 0
    assert out_lines(out) == [good1, good2]
    assert err.count("skipped") == 1 and "line 2" in err and "not valid JSON" in err


def _stream_into_early_reader(tmp_path: Path, *argv: str) -> tuple[int, bytes]:
    """Run ``pr-comment export`` into a reader that exits after 10 bytes; (exit code, stderr)."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ["HOME"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "XDG_STATE_HOME": os.environ["XDG_STATE_HOME"],
    }
    read_fd, write_fd = os.pipe()
    err_path = tmp_path / "stderr.txt"
    with err_path.open("wb") as err_file:
        reader = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read(10)"], stdin=read_fd
        )
        os.close(read_fd)
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "pr_comment", "export", *argv],
                stdout=write_fd,
                stderr=err_file,
                env=env,
            )
        finally:
            os.close(write_fd)
        try:
            code = child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
            raise
        reader.wait(timeout=20)
    return code, err_path.read_bytes()


@pytest.mark.parametrize("flags", [(), ("--json",)])
def test_a_closed_pipe_is_silent_and_exits_zero_for_a_real_process(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    put("log.jsonl", line(log_rec(details={"pad": "y" * 80})) * 20_000)
    code, err = _stream_into_early_reader(tmp_path, "--log", *flags)
    assert (code, err) == (0, b"")


def test_a_failing_stdout_write_is_an_internal_error_without_a_traceback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    put("log.jsonl", log_rec())
    real = cli._write  # pyright: ignore[reportPrivateUsage]

    def full(stream: Any, text: str) -> None:
        if text.startswith("{"):
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        real(stream, text)

    monkeypatch.setattr(cli, "_write", full)
    code, out, err = run(capsys, "--log")
    assert code == 1 and out == ""
    assert "unexpected OSError" in err and "No space left on device" in err
    assert "Traceback" not in err


def test_the_descriptor_is_closed_when_rendering_fails_before_the_stream_runs(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("audit.jsonl", audit_rec(ATT1))
    before = set(os.listdir("/proc/self/fd"))
    real = cli.render
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("render broke")
        return real(*args, **kwargs)

    monkeypatch.setattr(cli, "render", flaky)
    code, _, err = run(capsys)
    assert code == 1 and "unexpected RuntimeError" in err
    assert set(os.listdir("/proc/self/fd")) == before


def test_the_descriptor_is_closed_when_check_outcome_fails(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("audit.jsonl", audit_rec(ATT1))
    before = set(os.listdir("/proc/self/fd"))

    def broken(_: Any) -> Any:
        raise RuntimeError("check broke")

    monkeypatch.setattr(cli, "check_outcome", broken)
    code, _, _ = run(capsys)
    assert code == 1
    assert set(os.listdir("/proc/self/fd")) == before


def test_a_failure_before_the_first_flush_under_json_is_the_error_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("log.jsonl", log_rec())

    def broken(*_: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(export, "_emit_record", broken)
    code, out, _ = run(capsys, "--log", "--json")
    assert code == 1
    assert json.loads(out)["error"]["code"] == "internal"


def test_a_twelve_digit_fraction_is_printed_and_ordered_as_an_instant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = audit_rec(ATT1, "done", "2026-11-10T10:00:00.123456000001Z")
    after = audit_rec(ATT2, "done", "2026-11-10T10:00:00.123457000001Z")
    put("audit.jsonl", before, after)
    code, out, err = run(capsys)
    assert (code, err) == (0, "")
    assert out_lines(out) == [before, after]
    code, out, err = run(capsys, "--since", "2026-11-10T10:00:00.1234575Z")
    assert (code, err) == (0, "")
    assert out_lines(out) == [after]


def test_a_line_rewritten_between_the_passes_is_not_printed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = put("audit.jsonl", audit_rec(ATT1), audit_rec(ATT2))
    real = export._winners  # pyright: ignore[reportPrivateUsage]

    def rewriting(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        path.write_bytes(line(audit_rec(ATT1)) + line(audit_rec(ATT3)))
        return result

    monkeypatch.setattr(export, "_winners", rewriting)
    code, out, err = run(capsys)
    assert code == 0
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT1]
    assert err.count("skipped") == 1 and "line 2" in err


def test_a_skip_in_the_second_pass_past_the_cap_is_summarized(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    junk = [b"junk\n"] * (export.MAX_WARNINGS + 5)
    path = put("audit.jsonl", *junk, audit_rec(ATT1), audit_rec(ATT2))
    real = export._winners  # pyright: ignore[reportPrivateUsage]

    def rewriting(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        path.write_bytes(b"".join(junk) + line(audit_rec(ATT1)) + line(audit_rec(ATT3)))
        return result

    monkeypatch.setattr(export, "_winners", rewriting)
    code, out, err = run(capsys)
    assert code == 0
    assert [r["attempt_id"] for r in out_lines(out)] == [ATT1]
    assert "5 more" in err and "1 more" in err


# --- the real writers and the schemas ----------------------------------------------------------


def test_what_the_audit_and_log_writers_wrote_is_exported_unchanged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from pr_comment.jsonlog import LogWriter
    from tests.test_audit import full_run

    with files.open_state_dirs() as dirs:
        trail = full_run(dirs)
        log = LogWriter.open(dirs)
        log.write("refused", pr="example.com/acme/repo#7", attempt_id=trail.attempt_id)
        log.write("warning", details={"text": "café ‮"})
        log.close()
    written = [json.loads(x) for x in (state_dir() / "audit.jsonl").read_text().splitlines()]
    code, out, err = run(capsys, "--json")
    assert (code, err) == (0, "")
    assert json.loads(out) == written
    for record in written:
        validate("pr-comment/audit.json", record)
    logged = [json.loads(x) for x in (state_dir() / "log.jsonl").read_text().splitlines()]
    code, out, _ = run(capsys, "--json", "--log")
    assert json.loads(out) == logged
    validate("pr-comment/cli/export.json", json.loads(out))


def test_the_checked_values_match_the_schemas() -> None:
    schemas = load_schemas()
    audit = schemas["pr-comment/audit.json"]["properties"]
    log = schemas["pr-comment/log.json"]["properties"]
    assert list(export.AUDIT_RESULTS) == audit["result"]["enum"]
    assert list(export.AUDIT_ACTIONS) == audit["action"]["enum"]
    assert list(export.AUDIT_ACTOR_SOURCES) == audit["actor_source"]["enum"]
    assert list(export.LOG_EVENTS) == log["event"]["enum"]
    assert list(export.LOG_LEVELS) == log["level"]["enum"]
    assert export._ATTEMPT_ID_RE.pattern == audit["attempt_id"]["pattern"][1:-1]  # pyright: ignore[reportPrivateUsage]


def test_a_pre_write_os_error_under_json_keeps_the_error_envelope(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    put("log.jsonl", log_rec())

    def broken(*_: Any) -> Any:
        raise OSError(5, "Input/output error")

    silenced: list[bool] = []
    monkeypatch.setattr(export, "_emit_record", broken)
    monkeypatch.setattr(cli, "_silence_stdout", lambda: silenced.append(True))
    code, out, err = run(capsys, "--log", "--json")
    assert code == 1
    assert "Traceback" not in err
    assert json.loads(out)["error"]["code"] == "internal"
    assert silenced == []  # nothing was written, so stdout stays usable for the envelope
