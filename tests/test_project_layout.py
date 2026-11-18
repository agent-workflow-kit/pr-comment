"""Project-level guards: the `live` marker and the runtime dependency boundary."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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
    "httpcore",
    "markdown2",
    "mistletoe",
    "marko",
    "pycurl",
    "mdit_py_plugins",
    "markdown_it_py",
    "tornado.httpclient",
)


def _is_forbidden(name: str) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN)


def _dynamic_import_target(node: ast.Call) -> str | None:
    """The string argument of ``__import__('x')`` or ``importlib.import_module('x')``."""
    func = node.func
    called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    if called not in ("__import__", "import_module") or not node.args:
        return None
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return None


def forbidden_imports(source: str) -> list[str]:
    """Names of forbidden modules imported by ``source``.

    Covers lazy imports in functions and ``__import__('x')`` / ``import_module('x')`` calls with
    a string literal. A computed module name cannot be seen statically.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
        elif isinstance(node, ast.Call) and (target := _dynamic_import_target(node)):
            names = [target]
        found.extend(name for name in names if _is_forbidden(name))
    return found


def test_scanner_detects_forbidden_imports() -> None:
    assert forbidden_imports("import cmarkgfm") == ["cmarkgfm"]
    assert forbidden_imports("from jsonschema import validate") != []
    assert forbidden_imports("def f():\n    import markdown_it.token") != []
    assert forbidden_imports("from urllib import request") != []
    assert forbidden_imports("from urllib.request import urlopen") != []
    assert forbidden_imports("import requests as r") == ["requests"]
    assert forbidden_imports("import json, os\nfrom pathlib import Path") == []


@pytest.mark.parametrize(
    "source",
    [
        "import httpcore",
        "import markdown2",
        "import mistletoe",
        "import marko",
        "import pycurl",
        "from mdit_py_plugins import footnote",
        "import markdown_it_py",
        "from tornado import httpclient",
        "import tornado.httpclient",
        "__import__('requests')",
        "import importlib\nimportlib.import_module('cmarkgfm')",
        "from importlib import import_module\nimport_module('urllib.request')",
        "def f():\n    return __import__('jsonschema.validators')",
    ],
)
def test_scanner_detects_extended_and_dynamic_imports(source: str) -> None:
    assert forbidden_imports(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.parse",
        "from urllib.parse import quote",
        "import tornado.ioloop",
        "__import__('json')",
        "import importlib\nimportlib.import_module('pr_comment.cli')",
        "import_module(name)",
    ],
)
def test_scanner_allows_harmless_imports(source: str) -> None:
    assert forbidden_imports(source) == []


def test_no_runtime_module_imports_test_only_or_http_libraries() -> None:
    modules = sorted(SRC.rglob("*.py"))
    assert modules
    offenders = {
        str(path.relative_to(ROOT)): bad
        for path in modules
        if (bad := forbidden_imports(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_importing_the_cli_loads_no_forbidden_module(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "import pr_comment.cli\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "print(bad)\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _requirements(entries: list[Any]) -> list[Requirement]:
    """Parse requirement strings; ``include-group`` tables are not requirements."""
    return [Requirement(entry) for entry in entries if isinstance(entry, str)]


def _canonical(requirement: Requirement) -> str:
    return canonicalize_name(requirement.name)


def test_runtime_dependencies_are_exactly_pyyaml_and_pydantic() -> None:
    project = _pyproject()["project"]
    names = {_canonical(req) for req in _requirements(project["dependencies"])}
    assert names == {"pyyaml", "pydantic"}


def test_optional_dependencies_are_absent_or_free_of_banned_packages() -> None:
    extras = _pyproject()["project"].get("optional-dependencies", {})
    for entries in extras.values():
        for req in _requirements(entries):
            assert not _is_forbidden(_canonical(req).replace("-", "_")), req


def test_requirement_parsing_handles_extras_markers_and_specifiers() -> None:
    req = Requirement('pydantic[email]>=2.9; python_version >= "3.12"')
    assert _canonical(req) == "pydantic"
    assert _canonical(Requirement("PyYAML>=6")) == "pyyaml"


def test_cmarkgfm_is_pinned_exactly() -> None:
    groups = _pyproject()["dependency-groups"]
    pins = [
        req
        for entries in groups.values()
        for req in _requirements(entries)
        if _canonical(req) == "cmarkgfm"
    ]
    assert len(pins) == 1
    specs = list(pins[0].specifier)
    assert len(specs) == 1
    assert specs[0].operator in ("==", "===")
    assert "*" not in specs[0].version


def test_test_deps_live_in_the_test_group_and_dev_includes_it() -> None:
    groups = _pyproject()["dependency-groups"]
    test_names = {_canonical(req) for req in _requirements(groups["test"])}
    assert {"jsonschema", "referencing", "cmarkgfm", "rfc3339-validator", "packaging"} <= test_names
    assert {"include-group": "test"} in groups["dev"]
    dev_names = {_canonical(req) for req in _requirements(groups["dev"])}
    assert dev_names.isdisjoint({"jsonschema", "referencing", "cmarkgfm"})


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
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"),
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
