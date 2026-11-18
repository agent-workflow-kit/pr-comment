"""Golden-file helper. Goldens live in ``tests/golden/`` and hold no real names."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
        path.write_text(actual, encoding="utf-8", newline="\n")
        return
    if not path.is_file():
        raise AssertionError(f"missing golden file {name}; run pytest --regen-golden")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, f"golden mismatch for {name}; review and run pytest --regen-golden"
