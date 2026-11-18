"""`config_warning` lines reach log.jsonl through the CLI (issue #11, from #7)."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from pr_comment import cli, files
from tests.schema_harness import validate

PR = "example.test/o/r#7"


def state_dir() -> Path:
    return files.state_dir_path(dict(os.environ))


def log_records() -> list[dict[str, Any]]:
    path = state_dir() / "log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="ascii").splitlines()]


def write_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "cfg" / "config.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


@pytest.fixture
def stub_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(args: argparse.Namespace) -> Any:
        raise RuntimeError("stop")

    for name in list(cli.HANDLERS):
        monkeypatch.setitem(cli.HANDLERS, name, handler)


def test_a_config_that_sets_the_guarded_keys_writes_config_warning_lines(
    tmp_path: Path, stub_handlers: None
) -> None:
    config = write_config(tmp_path, "allow_auto_approve: true\nmentions:\n  policy: neutralize\n")
    cli.main(["--config", config, "comment", PR, "--body-file", "b", "--json"])
    (rec,) = log_records()
    validate("pr-comment/log.json", rec)
    assert rec["event"] == "config_warning" and rec["level"] == "warn"
    assert rec["details"]["file"] == config
    assert rec["details"]["keys"] == ["allow_auto_approve", "mentions.policy"]
    assert stat.S_IMODE(os.lstat(state_dir() / "log.jsonl").st_mode) == 0o600


def test_a_config_without_guarded_keys_writes_no_log_and_makes_no_state_dir(
    tmp_path: Path, stub_handlers: None
) -> None:
    config = write_config(tmp_path, "timeout: 30s\n")
    cli.main(["--config", config, "comment", PR, "--body-file", "b", "--json"])
    assert not state_dir().exists()


def test_each_run_writes_its_warnings_again(tmp_path: Path, stub_handlers: None) -> None:
    config = write_config(tmp_path, "allow_auto_approve: true\n")
    for _ in range(2):
        cli.main(["--config", config, "comment", PR, "--body-file", "b", "--json"])
    assert len(log_records()) == 2


def test_draft_check_and_export_read_no_config_and_write_no_log(
    tmp_path: Path, stub_handlers: None
) -> None:
    config = write_config(tmp_path, "allow_auto_approve: true\n")
    cli.main(["--config", config, "draft", "check", str(tmp_path / "nope.json"), "--json"])
    cli.main(["--config", config, "export", "--json"])
    assert log_records() == []


def test_an_unsafe_state_directory_does_not_stop_the_run_or_hide_the_config_error_path(
    tmp_path: Path, stub_handlers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    state = state_dir()
    state.mkdir(parents=True)
    state.chmod(0o755)  # not 0700: state_unsafe
    config = write_config(tmp_path, "allow_auto_approve: true\n")
    code = cli.main(["--config", config, "comment", PR, "--body-file", "b", "--json"])
    # The handler stub raises: the run reaches it, so the log failure did not end the run.
    assert code == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "internal"
    assert not (state / "log.jsonl").exists()


def test_a_symlinked_state_file_only_warns(
    tmp_path: Path, stub_handlers: None, capsys: pytest.CaptureFixture[str]
) -> None:
    state = state_dir()
    state.mkdir(parents=True)
    state.chmod(0o700)
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep\n")
    (state / "log.jsonl").symlink_to(victim)
    config = write_config(tmp_path, "allow_auto_approve: true\n")
    code = cli.main(["--config", config, "comment", PR, "--body-file", "b", "--json"])
    assert code == 1
    assert victim.read_bytes() == b"keep\n"
    assert "log.jsonl" in capsys.readouterr().err
