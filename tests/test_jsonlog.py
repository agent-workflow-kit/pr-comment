"""log.jsonl records (issue #11): schema validity, levels, and a failing log only warns."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import types
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pr_comment import appendfile, files, jsonlog
from pr_comment.config import ConfigWarning
from pr_comment.jsonlog import LogWriter, write_config_warnings
from tests.schema_harness import load_schemas, validate

ATTEMPT = "abcDEF123_-xyz"


@pytest.fixture
def dirs(tmp_path: Path) -> Iterator[files.StateDirs]:
    (tmp_path / "home").mkdir()
    env = {"HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "xdg")}
    with files.open_state_dirs(env) as opened:
        yield opened


def log_path(dirs: files.StateDirs) -> Path:
    return dirs.state_path / "log.jsonl"


def records(dirs: files.StateDirs) -> list[dict[str, Any]]:
    raw = log_path(dirs).read_bytes().decode("ascii")
    return [json.loads(line) for line in raw.splitlines()]


def proxy(**overrides: object) -> types.SimpleNamespace:
    return types.SimpleNamespace(**{**vars(os), **overrides})


def test_every_event_validates_against_the_schema(dirs: files.StateDirs) -> None:
    log = LogWriter.open(dirs)
    events = load_schemas()["pr-comment/log.json"]["properties"]["event"]["enum"]
    for event in events:
        assert log.write(event, pr="example.test/o/r#7", attempt_id=ATTEMPT, details={"n": 1})
    log.close()
    got = records(dirs)
    assert [r["event"] for r in got] == events
    for rec in got:
        validate("pr-comment/log.json", rec)


def test_the_file_is_created_0600_and_appended_to(dirs: files.StateDirs) -> None:
    LogWriter.open(dirs).write("warning")
    LogWriter.open(dirs).write("warning")
    assert stat.S_IMODE(os.lstat(log_path(dirs)).st_mode) == 0o600
    assert len(records(dirs)) == 2


def test_missing_fields_are_null(dirs: files.StateDirs) -> None:
    LogWriter.open(dirs).write("usage_error")
    (rec,) = records(dirs)
    assert (rec["pr"], rec["attempt_id"], rec["details"]) == (None, None, None)
    validate("pr-comment/log.json", rec)


@pytest.mark.parametrize(
    ("event", "level"),
    [
        ("refused", "warn"),
        ("vetoed", "warn"),
        ("escalated", "warn"),
        ("declined", "warn"),
        ("warning", "warn"),
        ("config_warning", "warn"),
        ("usage_error", "info"),
        ("finding_moved", "info"),
        ("suggestion_degraded", "info"),
        ("precheck", "info"),
        ("neutralizing_mismatch", "info"),
    ],
)
def test_the_default_level_follows_the_event(dirs: files.StateDirs, event: str, level: str) -> None:
    LogWriter.open(dirs).write(event)
    assert records(dirs)[0]["level"] == level


def test_the_caller_may_choose_the_level(dirs: files.StateDirs) -> None:
    log = LogWriter.open(dirs)
    log.write("refused", level="error")
    assert records(dirs)[0]["level"] == "error"


def test_debug_records_are_written_only_with_verbose(dirs: files.StateDirs) -> None:
    quiet = LogWriter.open(dirs)
    assert quiet.write("precheck", level="debug") is False
    assert not log_path(dirs).exists() or log_path(dirs).read_bytes() == b""
    loud = LogWriter.open(dirs, verbose=True)
    assert loud.write("precheck", level="debug") is True
    assert records(dirs)[0]["level"] == "debug"


def test_unknown_event_or_level_is_a_programming_error(dirs: files.StateDirs) -> None:
    log = LogWriter.open(dirs)
    with pytest.raises(ValueError):
        log.write("something_else")
    with pytest.raises(ValueError):
        log.write("warning", level="fatal")
    assert not log_path(dirs).exists() or log_path(dirs).read_bytes() == b""


def test_untrusted_details_are_escaped_to_one_ascii_line(dirs: files.StateDirs) -> None:
    evil = 'x\n{"event":"declined"}\r \x1b[2J\ud800‮ é'
    LogWriter.open(dirs).write("refused", details={"message": evil, "list": [evil]})
    raw = log_path(dirs).read_bytes()
    raw.decode("ascii")
    assert raw.count(b"\n") == 1 and b"\x1b" not in raw
    assert records(dirs)[0]["details"]["message"] == evil


def test_oversized_details_are_replaced_and_the_event_is_kept(dirs: files.StateDirs) -> None:
    log = LogWriter.open(dirs)
    assert log.write("refused", details={"blob": "a" * (jsonlog.MAX_LOG_LINE_BYTES * 2)})
    (rec,) = records(dirs)
    assert rec["details"] == {"truncated": True}
    validate("pr-comment/log.json", rec)


def test_unserializable_details_do_not_raise(
    dirs: files.StateDirs, caplog: pytest.LogCaptureFixture
) -> None:
    log = LogWriter.open(dirs)
    with caplog.at_level(logging.WARNING, logger="pr_comment"):
        assert log.write("warning", details={"x": float("nan")}) is False
        assert log.write("warning", details={"x": object()}) is False
    assert not log_path(dirs).exists() or log_path(dirs).read_bytes() == b""
    assert caplog.records


def test_a_symlinked_log_file_only_warns_and_is_not_followed(
    dirs: files.StateDirs, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep\n")
    log_path(dirs).symlink_to(victim)
    with caplog.at_level(logging.WARNING, logger="pr_comment"):
        log = LogWriter.open(dirs)
        assert log.write("warning") is False
    assert victim.read_bytes() == b"keep\n"
    assert any("log.jsonl" in r.getMessage() for r in caplog.records)


def test_a_full_disk_only_warns_once_and_never_raises(
    dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    log = LogWriter.open(dirs)

    def full(fd: int, data: bytes) -> int:
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(appendfile, "os", proxy(write=full))
    with caplog.at_level(logging.WARNING, logger="pr_comment"):
        assert log.write("warning") is False
        assert log.write("warning") is False
    assert len([r for r in caplog.records if "log.jsonl" in r.getMessage()]) == 1


def test_a_closed_or_disabled_writer_is_silent_and_never_raises(dirs: files.StateDirs) -> None:
    log = LogWriter.open(dirs)
    log.close()
    assert log.write("warning") is False
    assert LogWriter.disabled().write("warning") is False


def test_log_lines_are_not_fsynced(dirs: files.StateDirs, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(appendfile, "os", proxy(fsync=calls.append))
    LogWriter.open(dirs).write("warning")
    assert calls == []


def test_the_clock_is_injectable(dirs: files.StateDirs) -> None:
    moment = datetime(2030, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    LogWriter.open(dirs, clock=lambda: moment).write("warning")
    assert records(dirs)[0]["ts"] == "2030-01-02T03:04:05.678Z"


# --- config warnings -------------------------------------------------------------------------


def test_config_warnings_are_written_one_line_per_file(dirs: files.StateDirs) -> None:
    log = LogWriter.open(dirs)
    warnings = (
        ConfigWarning("/home/u/.config/pr-comment/config.yaml", ("allow_auto_approve",)),
        ConfigWarning("/etc/x\n/config.d/a.yaml", ("mentions.policy", "repos.o/r.mentions.allow")),
    )
    write_config_warnings(log, warnings)
    got = records(dirs)
    assert [r["event"] for r in got] == ["config_warning", "config_warning"]
    assert got[0]["details"] == {
        "file": "/home/u/.config/pr-comment/config.yaml",
        "keys": ["allow_auto_approve"],
    }
    assert got[1]["details"]["file"] == "/etc/x\n/config.d/a.yaml"
    assert got[1]["details"]["keys"] == ["mentions.policy", "repos.o/r.mentions.allow"]
    for rec in got:
        assert rec["level"] == "warn" and rec["pr"] is None and rec["attempt_id"] is None
        validate("pr-comment/log.json", rec)


def test_no_config_warnings_write_nothing(dirs: files.StateDirs) -> None:
    write_config_warnings(LogWriter.open(dirs), ())
    assert not log_path(dirs).exists() or log_path(dirs).read_bytes() == b""
