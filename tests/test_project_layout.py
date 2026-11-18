"""Project-level guards: the `live` marker and the runtime dependency boundary."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pr_comment"

# Test-only parsers and validators, Markdown parsers and HTTP libraries.
FORBIDDEN = (
    "cmarkgfm",
    "jsonschema",
    "referencing",
    "markdown_it",
    "markdown",
    "mistune",
    "commonmark",
    "requests",
    "httpx",
    "urllib3",
    "aiohttp",
    "httplib2",
    "http.client",
    "urllib.request",
)


def forbidden_imports(source: str) -> list[str]:
    """Names of forbidden modules imported by ``source`` (also lazy imports in functions)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        for name in names:
            if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN):
                found.append(name)
    return found


def test_scanner_detects_forbidden_imports() -> None:
    assert forbidden_imports("import cmarkgfm") == ["cmarkgfm"]
    assert forbidden_imports("from jsonschema import validate") != []
    assert forbidden_imports("def f():\n    import markdown_it.token") != []
    assert forbidden_imports("from urllib import request") != []
    assert forbidden_imports("from urllib.request import urlopen") != []
    assert forbidden_imports("import requests as r") == ["requests"]
    assert forbidden_imports("import json, os\nfrom pathlib import Path") == []


def test_no_runtime_module_imports_test_only_or_http_libraries() -> None:
    modules = sorted(SRC.rglob("*.py"))
    assert modules
    offenders = {
        str(path.relative_to(ROOT)): bad
        for path in modules
        if (bad := forbidden_imports(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_importing_the_cli_loads_no_forbidden_module() -> None:
    code = (
        "import sys\n"
        "import pr_comment.cli\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "print(bad)\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_dependencies_are_exactly_pyyaml_and_pydantic() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    names = {
        dep.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower()
        for dep in project["dependencies"]
    }
    assert names == {"pyyaml", "pydantic"}


def test_cmarkgfm_is_pinned_exactly() -> None:
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["dependency-groups"][
        "dev"
    ]
    pins = [d for d in dev if d.startswith("cmarkgfm")]
    assert len(pins) == 1
    assert "==" in pins[0]


def test_pytest_without_options_deselects_live_tests(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text(
        "import pytest\n"
        "def test_plain():\n    pass\n"
        "@pytest.mark.live\n"
        "def test_live():\n    raise AssertionError('live test must not run')\n",
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pytest",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            str(tmp_path / "test_sample.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "1 deselected" in result.stdout


def test_live_marker_is_registered() -> None:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ini = cfg["tool"]["pytest"]["ini_options"]
    assert any(m.startswith("live:") for m in ini["markers"])
    assert "not live" in ini["addopts"]


@pytest.mark.live
def test_live_marker_example_is_never_run_by_default() -> None:
    raise AssertionError("a live-marked test ran without -m live")
