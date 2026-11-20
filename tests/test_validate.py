"""``pr-comment validate`` (issue #111): offline, read-only config and forgectl check.

forgectl is always the fake of ``tests/fakebin`` (the autouse fixture leaves the ambient ``PATH``
empty), so no test reaches a real forgectl.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from pr_comment import validate as validate_module
from pr_comment.cli import main
from tests.forgectl_harness import FakeForgectl
from tests.schema_harness import validate

Doc = dict[str, Any]


def use_fake(monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl) -> None:
    for name, value in forgectl.environ().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(forgectl.directory / "run"))


def write_config(tmp_path: Path, text: str) -> str:
    path = tmp_path / "cfg" / "config.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, Doc, str]:
    code = main([*argv, "--json"])
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1  # a single JSON object on one line
    return code, json.loads(captured.out), captured.err


def tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_a_valid_config_exits_0_with_the_result_document(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl
) -> None:
    use_fake(monkeypatch, forgectl)
    code, doc, err = run(capsys, "validate")
    assert code == 0
    assert doc == {
        "valid": True,
        "config_path": None,
        "forgectl": {"version": "0.55.2", "minimum": "0.55.0"},
        "warnings": [],
    }
    validate("pr-comment/cli/validate.json", doc)
    assert "config ok" in err and "forgectl 0.55.2" in err


def test_the_config_path_is_reported_when_a_file_was_read(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    config = write_config(tmp_path, "timeout: 30s\n")
    code, doc, _ = run(capsys, "--config", config, "validate")
    assert code == 0 and doc["config_path"] == config and doc["warnings"] == []
    validate("pr-comment/cli/validate.json", doc)


def test_an_invalid_config_exits_2_naming_the_key(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    config = write_config(tmp_path, "timeout: -5s\n")
    code, doc, err = run(capsys, "--config", config, "validate")
    assert code == 2
    assert doc["error"]["code"] == "config_invalid"
    assert doc["error"]["details"] == {"key": "timeout"}
    validate("pr-comment/cli/error.json", doc)
    assert "timeout" in err
    assert forgectl.calls() == []  # the config fails before forgectl is looked at


@pytest.mark.parametrize(
    "text",
    [
        "",  # empty is a valid, empty config
        "[]\n",  # not a mapping
        "timeout: [1]\n",  # wrong type
        "unknown_key: 1\n",  # extra field
        "timeout: 5s\ntimeout: 6s\n",  # duplicate key
        "a: &x 1\nb: *x\n",  # alias
        "timeout: ‮5s\n",  # unicode control
        "\x00",  # NUL
        "mentions: {policy: nope}\n",
        "hooks: {pre-post: [{cmd: ''}]}\n",
        "x: " + "[" * 100 + "]" * 100 + "\n",  # nesting
        "k: " + "a" * 300_000 + "\n",  # over the size cap
    ],
)
def test_hostile_config_files_never_crash_validate(
    text: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    config = write_config(tmp_path, text)
    code, doc, _ = run(capsys, "--config", config, "validate")
    if text == "":
        assert code == 0 and doc["valid"] is True
    else:
        assert code == 2, doc
        assert doc["error"]["code"] == "config_invalid"
        assert isinstance(doc["error"]["details"]["key"], str)
    assert all(ord(c) < 128 for c in json.dumps(doc))


def test_a_missing_explicit_config_file_is_config_invalid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run(capsys, "--config", str(tmp_path / "nope.yaml"), "validate")
    assert code == 2 and doc["error"]["code"] == "config_invalid"


def test_the_environment_override_is_checked_and_named(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl
) -> None:
    use_fake(monkeypatch, forgectl)
    monkeypatch.setenv("PR_COMMENT_TIMEOUT", "soon")
    code, doc, _ = run(capsys, "validate")
    assert code == 2
    assert doc["error"]["details"] == {"key": "PR_COMMENT_TIMEOUT"}


def test_a_config_that_sets_a_guarded_key_is_a_warning_and_writes_no_log_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    config = write_config(tmp_path, "allow_auto_approve: true\nmentions:\n  policy: allow\n")
    code, doc, err = run(capsys, "--config", config, "validate")
    assert code == 0
    assert doc["warnings"] == ["config_warning"]
    validate("pr-comment/cli/validate.json", doc)
    assert "allow_auto_approve" in err and "mentions.policy" in err
    state = Path(os.environ["XDG_STATE_HOME"])
    assert not state.exists()  # no state directory, so no log.jsonl and no audit.jsonl


def test_several_warnings_list_each_code_once(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    forgectl.set_version("0.99.0")
    config = write_config(tmp_path, "allow_auto_approve: true\n")
    (tmp_path / "cfg" / "config.d").mkdir()
    extra = tmp_path / "cfg" / "config.d" / "a.yaml"
    extra.write_text("mentions:\n  policy: allow\n", encoding="utf-8")
    extra.chmod(0o600)
    code, doc, _ = run(capsys, "--config", config, "validate")
    assert code == 0
    assert doc["warnings"] == ["config_warning", "forgectl_untested_version"]
    validate("pr-comment/cli/validate.json", doc)


@pytest.mark.parametrize(
    ("version", "warnings"),
    [
        ("0.55.0", []),
        ("0.55.2", []),
        ("0.55.999", []),
        ("0.55.2-rc.1", []),
        ("0.55.2+build.5", []),
        ("0.56.0", ["forgectl_untested_version"]),
        ("1.0.0", ["forgectl_untested_version"]),
    ],
)
def test_versions_at_or_above_the_minimum_pass(
    version: str,
    warnings: list[str],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    forgectl.set_version(version)
    code, doc, _ = run(capsys, "validate")
    assert code == 0
    assert doc["warnings"] == warnings
    assert doc["forgectl"]["version"] == version.split("-")[0].split("+")[0]
    validate("pr-comment/cli/validate.json", doc)


@pytest.mark.parametrize(
    "version",
    [
        "0.54.9",
        "0.1.0",
        "0.0.0",
        "0.55",  # not three numbers
        "0.55.x",
        "v0.55.2",
        "0.55.2 extra",
        "-1.55.2",
        "",
        "latest",
        "0.55.2‮",  # a bidi control (sanitized away from the doc, never accepted)
        "0.55.99999999999999999999",  # beyond the digit cap
        "9" * 5000,
    ],
)
def test_a_version_below_the_minimum_or_unreadable_is_a_contract_mismatch(
    version: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    forgectl.set_version(version)
    code, doc, err = run(capsys, "validate")
    assert code == 12, doc
    error = doc["error"]
    assert error["code"] == "lower_contract_mismatch"
    assert error["details"]["command"] == "--version"
    assert error["landed"] is False and error["retry"] == "same_command"
    validate("pr-comment/cli/error.json", doc)
    assert len(err) < 2000
    assert all(ord(c) < 128 for c in json.dumps(doc))


def test_a_missing_forgectl_is_tool_missing_with_the_install_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, doc, _ = run(capsys, "validate")  # the autouse fixture left PATH empty
    assert code == 12
    assert doc["error"]["code"] == "tool_missing"
    assert "install" in doc["error"]["details"]
    validate("pr-comment/cli/error.json", doc)


def test_a_non_executable_forgectl_bin_is_tool_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, f"forgectl_bin: {tmp_path / 'nothing-here'}\n")
    code, doc, _ = run(capsys, "--config", config, "validate")
    assert code == 12 and doc["error"]["code"] == "tool_missing"


def test_only_forgectl_version_runs_and_nothing_is_written(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forgectl: FakeForgectl,
) -> None:
    use_fake(monkeypatch, forgectl)
    config = write_config(tmp_path, "allow_auto_approve: true\n")
    before = tree(tmp_path)
    code, _, _ = run(capsys, "--config", config, "validate")
    assert code == 0
    assert [c.subcommand for c in forgectl.calls()] == ["--version"]
    # Nothing new under tmp_path (the private HOME, state and config homes included), apart from
    # the fake's own call log.
    created = [p for p in tree(tmp_path) if p not in before and "forgectl-fake" not in p]
    assert created == []
    assert not Path(os.environ["XDG_STATE_HOME"]).exists()


def test_the_run_does_not_read_stdin(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl
) -> None:
    use_fake(monkeypatch, forgectl)

    class Boom:
        def read(self, *_: object) -> str:
            raise AssertionError("stdin was read")

        def isatty(self) -> bool:
            return False

        def fileno(self) -> int:
            raise OSError

    monkeypatch.setattr("sys.stdin", Boom())
    code, _, _ = run(capsys, "validate")
    assert code == 0


def test_no_token_or_environment_value_reaches_the_output(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl
) -> None:
    use_fake(monkeypatch, forgectl)
    monkeypatch.setenv("GH_TOKEN", "canary-token-value")
    monkeypatch.setenv("GITLAB_TOKEN", "canary-token-value")
    code = main(["validate", "--json", "--verbose"])
    captured = capsys.readouterr()
    assert code == 0
    assert "canary-token-value" not in captured.out + captured.err


@pytest.mark.parametrize("extra", [["stray"], ["--since", "7d"], ["--log"], ["--probe"]])
def test_validate_takes_no_arguments_of_its_own(
    extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["validate", *extra, "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 2 and doc["error"]["code"] == "usage"


def test_quiet_drops_the_human_lines_but_not_the_document(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl
) -> None:
    use_fake(monkeypatch, forgectl)
    code = main(["validate", "--json", "--quiet"])
    captured = capsys.readouterr()
    assert code == 0 and captured.err == ""
    assert json.loads(captured.out)["valid"] is True


def test_without_json_the_result_stays_off_stdout(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, forgectl: FakeForgectl
) -> None:
    use_fake(monkeypatch, forgectl)
    code = main(["validate"])
    captured = capsys.readouterr()
    assert code == 0 and captured.out == "" and "config ok" in captured.err


def test_the_minimum_is_the_start_of_the_pinned_forgectl_range() -> None:
    pin = json.loads((Path(__file__).parent / "forgectl_pin.json").read_text(encoding="utf-8"))
    major, minor, _patch = (int(n) for n in pin["version"].split("."))
    assert validate_module.MINIMUM_FORGECTL == (major, minor, 0)
    assert validate_module.UNTESTED_FROM == (major, minor + 1, 0)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("forgectl 0.55.2", (0, 55, 2)),
        ("forgectl 10.200.3000-rc1", (10, 200, 3000)),
        (None, None),
        ("", None),
        ("forgectl", None),
        ("forgectl 0.55.2\n", None),  # the gateway gives one line; a newline is not accepted
        ("forgectl 0.55.2 ", None),
        ("forgectl  0.55.2", None),
        ("forgectl ０.55.2", None),  # a fullwidth digit is not a digit here
        ("forgectl ٠.55.2", None),  # an Arabic-Indic digit is not a digit here
        ("FORGECTL 0.55.2", None),
        ("other 0.55.2", None),
        ("forgectl 0.55.2-" + "a" * 100_000, (0, 55, 2)),
    ],
)
def test_parse_forgectl_version(line: str | None, expected: tuple[int, int, int] | None) -> None:
    assert validate_module.parse_forgectl_version(line) == expected
