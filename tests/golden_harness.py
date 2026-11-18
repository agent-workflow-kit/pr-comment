"""Golden-file helper. Goldens live in ``tests/golden/`` and hold no real names."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def golden_path(name: str) -> Path:
    """Resolve ``name`` inside ``tests/golden/``; refuse absolute paths and ``..`` escapes."""
    path = (GOLDEN_DIR / name).resolve()
    if not path.is_relative_to(GOLDEN_DIR.resolve()) or path == GOLDEN_DIR.resolve():
        raise ValueError(f"golden name escapes tests/golden/: {name!r}")
    return path


def render_json(document: Any) -> str:
    """Stable text form of a JSON document: sorted keys, 2-space indent, trailing newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check_golden(name: str, actual: str, *, regenerate: bool) -> None:
    """Compare ``actual`` with ``tests/golden/<name>``.

    With ``regenerate`` the file is (re)written instead. A missing file without
    ``regenerate`` fails, so a new golden is never created by accident.
    """
    path = golden_path(name)
    if regenerate:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(actual.encode("utf-8", errors="surrogatepass"))
        return
    if not path.is_file():
        raise AssertionError(f"missing golden file {name}; run pytest --regen-golden")
    expected = path.read_bytes().decode("utf-8", errors="surrogatepass")
    assert actual == expected, f"golden mismatch for {name}; review and run pytest --regen-golden"


def refuse_regen_in_ci(env: Mapping[str, str], *, regenerate: bool) -> None:
    """Regenerating goldens rewrites the expected output; CI must only compare."""
    if regenerate and env.get("CI"):
        raise pytest.UsageError("--regen-golden is refused when the CI environment variable is set")


# Placeholder owners and hosts that may appear in goldens; anything else looks like a real name.
PLACEHOLDER_OWNERS = frozenset({"example", "octo-org", "octocat", "acme"})
SHARED_HOSTS = frozenset({"github.com", "gitlab.com"})
PLACEHOLDER_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:example|example\.com|example\.org|example\.net)$"
)
FILE_EXTENSIONS = frozenset(
    {
        "json",
        "md",
        "txt",
        "py",
        "yaml",
        "yml",
        "toml",
        "html",
        "sh",
        "log",
        "git",
        "patch",
        "diff",
        "lock",
    }
)
_HOST_RE = re.compile(r"(?<![\w.-])((?:[a-z0-9-]+\.)+[a-z]{2,})(?![\w-])", re.IGNORECASE)
_OWNER_RE = re.compile(r"^[/:]+([\w.-]+)")


def find_real_names(text: str) -> list[str]:
    """Hosts and owners in ``text`` that are not placeholders; empty when ``text`` is clean.

    A dotted token is a host unless its last label is a common file extension. ``example`` hosts
    are always fine; ``github.com`` and ``gitlab.com`` only with a placeholder owner.
    """
    problems: list[str] = []
    for match in _HOST_RE.finditer(text):
        host = match.group(1).lower()
        if host.rsplit(".", 1)[-1] in FILE_EXTENSIONS:
            continue
        if PLACEHOLDER_HOST_RE.match(host):
            continue
        if host in SHARED_HOSTS:
            owner = _OWNER_RE.match(text[match.end() :])
            if owner and owner.group(1).lower() in PLACEHOLDER_OWNERS:
                continue
            problems.append(f"{host}{owner.group(0) if owner else ''}")
            continue
        problems.append(host)
    return problems
