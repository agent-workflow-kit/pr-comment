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
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests import fake_forgectl as ff
from tests import forgectl_harness
from tests.fakes import Tracker, alive, make_fake, record, recorded
from tests.forgectl_harness import PINNED_VERSION, PR, FakeForgectl, fail

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


_CHOICES = re.compile(r"\(choose from [^)]*\)")


def _python_neutral(text: str) -> str:
    """argparse quotes the choices of an invalid command from Python 3.13 on, not before."""
    return _CHOICES.sub("(choose from ...)", text)


def observed(proc: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
    stdout = proc.stdout.decode("utf-8")
    try:
        document: Any = json.loads(stdout) if stdout.strip() else None
    except ValueError:
        document = None
    return json.loads(
        _python_neutral(
            json.dumps(
                {
                    "exit": proc.returncode,
                    "stdout": document if document is not None else stdout,
                    "stderr": proc.stderr.decode("utf-8"),
                }
            )
        )
    )


def command_of(argv: list[str]) -> str:
    """The forgectl command of an argv, wherever the global flags put it."""
    return next(a for a in argv if a in ff.COMMAND_ORDER)


def materialize(case: dict[str, Any], directory: Path) -> list[str]:
    """The case's argv with ``{tmp}`` made a directory holding the case's files."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in case.get("files", {}).items():
        (directory / name).write_text(text, encoding="utf-8")
    return [a.replace("{tmp}", str(directory)) for a in case["argv"]]


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    return json.loads(_python_neutral(json.dumps(case["expect"])))


def test_the_recording_is_of_the_pinned_release() -> None:
    assert RECORDED["_meta"]["recorded_from"] == f"forgectl {PINNED_VERSION}"
    assert len(CASES) > 150
    assert len({c["name"] for c in CASES}) == len(CASES)
    assert sum("reaches_host" in c for c in CASES) > 50  # what forgectl accepts, not only refuses


REFUSED = [c for c in CASES if "reaches_host" not in c]
ACCEPTED = [c for c in CASES if "reaches_host" in c]


@pytest.mark.parametrize("case", REFUSED, ids=[c["name"] for c in REFUSED])
def test_the_fake_refuses_as_the_real_forgectl_did(
    case: dict[str, Any], forgectl: FakeForgectl, tmp_path: Path
) -> None:
    forgectl.allow_usage_errors = True  # these cases are the argv forgectl refuses
    proc = forgectl.run(materialize(case, tmp_path / "files"), stdin=stdin_bytes(case["stdin"]))
    assert observed(proc) == _expected(case)


@pytest.mark.parametrize("case", ACCEPTED, ids=[c["name"] for c in ACCEPTED])
def test_the_fake_accepts_what_the_real_forgectl_accepted(
    case: dict[str, Any], forgectl: FakeForgectl, tmp_path: Path
) -> None:
    """The real forgectl got past its command line and input checks and asked a host (which, with
    no ``gh`` or ``glab``, failed with tool_missing). The fake must get as far: the scripted host
    answer is what ends the call."""
    argv = materialize(case, tmp_path / "files")
    install = "brew install gh (or see https://cli.github.com/)"
    forgectl.script(
        fail(command_of(argv), "tool_missing", "gh was not found", details={"install": install})
    )
    proc = forgectl.run(argv, stdin=stdin_bytes(case["stdin"]))
    assert proc.returncode == 12, proc.stderr
    assert out_code(proc) == "tool_missing"
    assert forgectl.calls()[0].step == 0  # it reached the host


def out_code(proc: subprocess.CompletedProcess[bytes]) -> str:
    return json.loads(proc.stdout)["error"]["code"]


def _real_forgectl() -> str:
    # the tests run with an empty PATH; the real one was captured when the harness was imported
    found = shutil.which("forgectl", path=forgectl_harness.REAL_PATH)
    if found is None:
        pytest.skip("no forgectl on PATH")
    version = subprocess.run([found, "--version"], capture_output=True, text=True, check=True)
    if version.stdout.strip() != f"forgectl {PINNED_VERSION}":
        pytest.skip(f"forgectl is {version.stdout.strip()!r}, the pin is {PINNED_VERSION}")
    return found


def test_the_live_lookup_uses_the_path_captured_before_the_tests_empty_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stand-in on the captured PATH is found even though this test's own PATH is empty; it is
    only looked up and asked its version, never given a case."""
    bin_dir = tmp_path / "real-bin"
    bin_dir.mkdir()
    stand_in = bin_dir / "forgectl"
    stand_in.write_text(f"#!/bin/sh\necho 'forgectl {PINNED_VERSION}'\n", encoding="utf-8")
    stand_in.chmod(0o755)
    monkeypatch.setattr(forgectl_harness, "REAL_PATH", str(bin_dir))
    assert shutil.which("forgectl") is None  # the autouse empty PATH
    assert _real_forgectl() == str(stand_in)


def test_the_captured_path_is_the_real_one_not_the_empty_one() -> None:
    assert forgectl_harness.REAL_PATH and "empty-path" not in forgectl_harness.REAL_PATH


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
        [binary, *materialize(case, tmp_path / "files")],
        input=stdin_bytes(case["stdin"]),
        capture_output=True,
        env=env,
        cwd="/",
        timeout=120,
        check=False,
    )
    if "reaches_host" in case:
        assert case["expect"] == {"reaches_host": True}
        assert proc.returncode == 12 and out_code(proc) == "tool_missing", proc.stdout
    else:
        assert observed(proc) == _expected(case)


@pytest.mark.live
@pytest.mark.parametrize(
    ("signum", "exit_code"),
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)],
)
def test_the_real_forgectl_exits_128_plus_n_with_no_output_on_a_signal_before_a_write(
    signum: int, exit_code: int, tmp_path: Path, tracker: Tracker
) -> None:
    """The fake's signal behavior before a write, checked on the real binary: a stand-in ``gh``
    that waits (no network, no credentials) keeps forgectl in a read when the signal arrives. The
    stand-in's pid is recorded, so only it is checked afterwards, never matched by name."""
    binary = _real_forgectl()
    tracker.watch(tmp_path)
    make_fake(tmp_path, "open(D + '/ready', 'w').close(); time.sleep(30)", name="gh")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / "state"),
        "PATH": str(tmp_path / "bin"),
    }
    proc = subprocess.Popen(
        [binary, "show", "--json", PR],
        env=env,
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    record(tmp_path, proc.pid)
    deadline = time.monotonic() + 30
    while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (tmp_path / "ready").exists(), "forgectl never started the stand-in gh"
    os.kill(proc.pid, signum)
    stdout, stderr = proc.communicate(timeout=30)
    assert (proc.returncode, stdout, stderr) == (exit_code, b"", b"")
    gh = [r for r in recorded(tmp_path) if r[0] != proc.pid and r[1] != proc.pid]
    assert gh and not any(alive(pid, start) for pid, _, start in gh)  # forgectl ended its child
