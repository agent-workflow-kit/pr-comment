"""The fake forgectl answers as the real forgectl does, case by case.

``tests/forgectl_cases.json`` holds argv and stdin cases that end before any host is read (the
command line, the closed host list, human-only flags, the post input), with the exact exit code,
stdout and stderr the real forgectl (the pinned release) gave when they were recorded. The fake must
reproduce each one; the ``live`` test replays them against a real forgectl, so a change in forgectl
that the fake does not follow fails there. Nothing here reaches a network: the real binary runs in a
private HOME with a ``PATH`` that holds no ``gh`` or ``glab``.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.forgectl_harness import PINNED_VERSION, FakeForgectl

CASES_FILE = Path(__file__).resolve().parent / "forgectl_cases.json"
RECORDED: dict[str, Any] = json.loads(CASES_FILE.read_text(encoding="utf-8"))
CASES: list[dict[str, Any]] = RECORDED["cases"]


def stdin_bytes(spec: dict[str, Any]) -> bytes:
    if "text" in spec:
        return spec["text"].encode("utf-8", "surrogatepass")
    if "b64" in spec:
        return base64.b64decode(spec["b64"])
    out = b""
    for part in spec["parts"]:
        if isinstance(part, str):
            out += part.encode("utf-8")
        else:
            unit, count = part["repeat"]
            out += unit.encode("utf-8") * count
    return out


def observed(proc: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    stdout = proc.stdout.decode("utf-8")
    try:
        document: Any = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        document = None
    return {
        "exit": proc.returncode,
        "stdout": document if document is not None else stdout,
        "stderr": proc.stderr.decode("utf-8"),
    }


def test_the_recording_is_of_the_pinned_release() -> None:
    assert RECORDED["_meta"]["recorded_from"] == f"forgectl {PINNED_VERSION}"
    assert len(CASES) > 100
    assert len({c["name"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_fake_answers_as_the_real_forgectl_did(
    case: dict[str, Any], forgectl: FakeForgectl
) -> None:
    forgectl.allow_usage_errors = True  # these cases are the argv forgectl refuses
    proc = forgectl.run(case["argv"], stdin=stdin_bytes(case["stdin"]))
    assert observed(proc) == case["expect"]


def _real_forgectl() -> str:
    found = shutil.which("forgectl")
    if found is None:
        pytest.skip("no forgectl on PATH")
    version = subprocess.run([found, "--version"], capture_output=True, text=True, check=True)
    if version.stdout.strip() != f"forgectl {PINNED_VERSION}":
        pytest.skip(f"forgectl is {version.stdout.strip()!r}, the pin is {PINNED_VERSION}")
    return found


@pytest.mark.live
@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_real_forgectl_still_answers_as_recorded(case: dict[str, Any], tmp_path: Path) -> None:
    """Needs the pinned forgectl installed; runs it with no credentials and no ``gh``/``glab``, so
    no case can reach a host. A failure means forgectl changed and the fake (and the recording)
    must follow."""
    binary = _real_forgectl()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / "state"),
        "PATH": str(tmp_path / "empty-bin"),
    }
    proc = subprocess.run(
        [binary, *case["argv"]],
        input=stdin_bytes(case["stdin"]),
        capture_output=True,
        env=env,
        cwd="/",
        timeout=120,
        check=False,
    )
    assert observed(proc) == case["expect"]
