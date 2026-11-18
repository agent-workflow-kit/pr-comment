from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import golden_harness
from tests.golden_harness import (
    check_golden,
    find_real_names,
    golden_path,
    refuse_regen_in_ci,
    render_json,
)


@pytest.fixture
def golden_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(golden_harness, "GOLDEN_DIR", tmp_path)
    return tmp_path


def test_regenerate_writes_then_compare_passes(golden_dir: Path) -> None:
    check_golden("sub/a.txt", "hello\n", regenerate=True)
    assert (golden_dir / "sub" / "a.txt").read_text() == "hello\n"
    check_golden("sub/a.txt", "hello\n", regenerate=False)


def test_mismatch_fails(golden_dir: Path) -> None:
    check_golden("a.txt", "one\n", regenerate=True)
    with pytest.raises(AssertionError, match="golden mismatch"):
        check_golden("a.txt", "two\n", regenerate=False)


def test_missing_golden_fails_without_regenerate(golden_dir: Path) -> None:
    with pytest.raises(AssertionError, match="missing golden"):
        check_golden("absent.txt", "x", regenerate=False)
    assert not (golden_dir / "absent.txt").exists()


@pytest.mark.parametrize("name", ["../escape.txt", "/etc/passwd", "a/../../x", "."])
def test_names_outside_golden_dir_are_refused(golden_dir: Path, name: str) -> None:
    with pytest.raises(ValueError):
        golden_path(name)


def test_render_json_is_stable_and_unicode_safe() -> None:
    assert render_json({"b": 1, "a": "é"}) == '{\n  "a": "é",\n  "b": 1\n}\n'


def test_golden_fixture_uses_regen_flag(golden, golden_json) -> None:  # noqa: ANN001
    # Comparing against a committed golden: proves the fixtures wire up to tests/golden/.
    golden_json("harness/smoke.json", {"ok": True})


@pytest.mark.parametrize("text", ["a\r\nb\n", "\r", "a\rb", "\ud800", "x\udfffy", "  \u0085"])
def test_round_trip_is_byte_exact(golden_dir: Path, text: str) -> None:
    check_golden("rt.txt", text, regenerate=True)
    check_golden("rt.txt", text, regenerate=False)
    assert (golden_dir / "rt.txt").read_bytes() == text.encode("utf-8", errors="surrogatepass")


def test_crlf_golden_does_not_match_lf_text(golden_dir: Path) -> None:
    check_golden("crlf.txt", "a\r\nb\n", regenerate=True)
    with pytest.raises(AssertionError, match="golden mismatch"):
        check_golden("crlf.txt", "a\nb\n", regenerate=False)


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/a",
        "see octocat/hello#3 at github.com/octocat/hello/pull/3",
        "git@github.com:acme/widgets.git",
        "gitlab.example/octo-org/repo",
        "host: ci.example\nfile: defs.json notes.md",
        "plain text with no hosts",
    ],
)
def test_scanner_accepts_placeholders(text: str) -> None:
    assert find_real_names(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "https://git.realcorp.io/team/repo",
        "github.com/realowner/repo#1",
        "git@github.com:realowner/repo.git",
        "mail me at x@corp.internal",
        "https://example.com.evil.net/x",
        "https://git.acmehost.sh/team/repo",
        "git@build.acmehost.md:team/repo.git",
    ],
)
def test_scanner_flags_real_looking_names(text: str) -> None:
    assert find_real_names(text) != []


def test_committed_goldens_hold_no_real_names() -> None:
    offenders = {
        path.relative_to(golden_harness.GOLDEN_DIR).as_posix(): bad
        for path in sorted(golden_harness.GOLDEN_DIR.rglob("*"))
        if path.is_file()
        and (bad := find_real_names(path.read_bytes().decode("utf-8", errors="replace")))
    }
    assert offenders == {}


def test_regen_is_refused_in_ci() -> None:
    with pytest.raises(pytest.UsageError, match="CI"):
        refuse_regen_in_ci({"CI": "true"}, regenerate=True)
    refuse_regen_in_ci({"CI": "true"}, regenerate=False)
    refuse_regen_in_ci({}, regenerate=True)
    refuse_regen_in_ci({"CI": ""}, regenerate=True)


def test_regen_flag_fails_the_pytest_run_in_ci(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "CI": "true",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--regen-golden",
            "-p",
            "no:cacheprovider",
            "tests/test_golden_harness.py",
        ],
        cwd=golden_harness.GOLDEN_DIR.parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode != 0
    assert "CI" in result.stdout + result.stderr
