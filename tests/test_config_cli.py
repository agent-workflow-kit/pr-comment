"""The config loader through the CLI: config_invalid envelopes and what loads when."""

from __future__ import annotations

import argparse
import json
from fractions import Fraction
from pathlib import Path

import pytest

from pr_comment import cli, grammars
from pr_comment.config import LoadedConfig

PR = "example.test/o/r#7"


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def write_config(tmp_path: Path, text: str, mode: int = 0o644) -> str:
    path = tmp_path / "cfg" / "config.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return str(path)


def test_negative_timeout_is_config_invalid_with_the_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, "timeout: -5s\n")
    code, out, err = run(capsys, "--json", "--config", config, "comment", PR, "--body-file", "-")
    error = json.loads(out)["error"]
    assert code == 2
    assert error["code"] == "config_invalid"
    assert error["details"] == {"key": "timeout"}
    assert "timeout" in err


def test_forgectl_config_with_a_leading_dash_is_config_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, "forgectl_config: -x\n")
    code, out, _ = run(capsys, "--json", "--config", config, "approve", PR)
    assert code == 2
    assert json.loads(out)["error"]["details"] == {"key": "forgectl_config"}


def test_environment_timeout_is_config_invalid_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "-5s")
    code, out, _ = run(capsys, "--json", "resolve", PR, "--thread", "gh:rt:abc")
    assert code == 2
    assert json.loads(out)["error"]["details"] == {"key": "PR_COMMENT_TIMEOUT"}


def test_missing_config_flag_file_is_config_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "--json", "--config", str(tmp_path / "nope.yaml"), "approve", PR)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "config_invalid"


def test_world_writable_config_is_config_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, "{}\n", mode=0o666)
    code, out, err = run(capsys, "--json", "--config", config, "approve", PR)
    assert code == 2
    assert json.loads(out)["error"]["code"] == "config_invalid"
    assert "writable by everyone" in err


def test_human_message_is_one_sanitized_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, '"bad\\u202ekey": 1\n')
    code, _, err = run(capsys, "--config", config, "approve", PR)
    assert code == 2
    assert "‮" not in err
    assert len(err.splitlines()) == 1


def test_valid_config_loads_and_the_handler_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, "timeout: 5s\n")
    code, out, _ = run(capsys, "--json", "--config", config, "draft", "new", PR)
    # No handler is written yet (and this one would never reach forgectl): the dispatch reaches it
    # and reports `internal`.
    assert code == 1
    assert json.loads(out)["error"]["code"] == "internal"


def test_draft_check_reads_no_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, "timeout: -5s\n")
    code, out, _ = run(capsys, "--json", "--config", config, "draft", "check", "x.json")
    assert code == 1  # the unwritten handler ran: the invalid config was never read
    assert json.loads(out)["error"]["code"] == "internal"


def test_export_reads_no_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_config(tmp_path, "timeout: -5s\n")
    code, out, _ = run(capsys, "--json", "--config", config, "export")
    assert code == 1  # the unwritten handler ran: the invalid config was never read
    assert json.loads(out)["error"]["code"] == "internal"


def test_hostile_key_is_sanitized_in_message_and_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, '"bad\\u202e\\u001b[2Jkey": 1\n')
    code, out, err = run(capsys, "--json", "--config", config, "approve", PR)
    error = json.loads(out)["error"]
    assert code == 2
    for text in (error["message"], error["details"]["key"], err):
        assert "\u202e" not in text
        assert "\x1b" not in text
    assert "<U+202E>" in error["details"]["key"]


def test_non_utf8_config_path_is_sanitized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = str(tmp_path / "no\udcffpe.yaml")  # the str form of the bytes \xff
    code, out, err = run(capsys, "--json", "--config", path, "approve", PR)
    error = json.loads(out)["error"]
    assert code == 2
    assert error["code"] == "config_invalid"
    assert "<U+DCFF>" in error["details"]["key"]
    assert "<U+DCFF>" in error["message"]
    out.encode("utf-8")
    err.encode("utf-8")


def test_usage_errors_win_before_config_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, "timeout: -5s\n")
    code, out, _ = run(
        capsys, "--json", "--config", config, "comment", "-bad#1", "--body-file", "-"
    )
    assert code == 2
    assert json.loads(out)["error"]["code"] == "invalid_pr"


def test_config_is_loaded_once_into_the_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[LoadedConfig] = []

    def handler(args: argparse.Namespace) -> cli.Outcome:
        seen.append(args.loaded_config)
        return cli.Outcome(0)

    monkeypatch.setitem(cli.HANDLERS, "approve", handler)
    config = write_config(tmp_path, "timeout: 5s\nme: {name: carol}\n")
    code, _, _ = run(capsys, "--config", config, "approve", PR)
    assert code == 0
    assert len(seen) == 1
    assert seen[0].config.timeout == "5s"
    assert seen[0].config.me.name == "carol"


# --- grammars.duration_seconds ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        (30, Fraction(30)),
        (0.5, Fraction(1, 2)),
        ("30", Fraction(30)),
        ("30s", Fraction(30)),
        ("250ms", Fraction(1, 4)),
        ("2m", Fraction(120)),
        ("1.5h", Fraction(5400)),
        ("1d", Fraction(86400)),
    ],
)
def test_duration_seconds(value: object, seconds: Fraction) -> None:
    assert grammars.duration_seconds(value) == seconds


@pytest.mark.parametrize("value", [0, -1, "0s", "-5s", "x", True, None, float("nan"), float("inf")])
def test_duration_seconds_refuses_what_check_timeout_refuses(value: object) -> None:
    with pytest.raises(grammars.GrammarError):
        grammars.duration_seconds(value)
