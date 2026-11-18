from __future__ import annotations

from pathlib import Path

import pytest

from tests import golden_harness
from tests.golden_harness import check_golden, golden_path, render_json


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
