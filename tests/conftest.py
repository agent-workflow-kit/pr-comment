from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import Tracker
from tests.forgectl_harness import FakeForgectl
from tests.golden_harness import check_golden, refuse_regen_in_ci, render_json


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--regen-golden",
        action="store_true",
        default=False,
        help="rewrite tests/golden/ files instead of comparing against them",
    )


def pytest_configure(config: pytest.Config) -> None:
    refuse_regen_in_ci(os.environ, regenerate=bool(config.getoption("--regen-golden")))


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test runs with a private HOME, XDG_CONFIG_HOME and XDG_STATE_HOME and no PR_COMMENT_*
    variable, so the developer's real config, state and environment never reach a test."""
    home = tmp_path / "private-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    for name in [n for n in os.environ if n.startswith("PR_COMMENT_")]:
        monkeypatch.delenv(name)
    return home


@pytest.fixture
def default_signals() -> Iterator[None]:
    """A test that sends SIGINT, SIGTERM or SIGHUP to itself needs them not ignored: pr-comment
    leaves an ignored signal alone, as a background job's shell sets them. Whichever of the three
    is ignored when the test starts gets its default action for the test and is restored after."""
    saved: dict[int, Any] = {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        if signal.getsignal(sig) == signal.SIG_IGN:
            saved[sig] = signal.SIG_IGN
            signal.signal(sig, signal.SIG_DFL)
    try:
        yield
    finally:
        for sig, previous in saved.items():
            signal.signal(sig, previous)


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> Callable[[str, str], None]:
    """``golden(name, text)`` compares text with ``tests/golden/<name>``."""
    regenerate = bool(request.config.getoption("--regen-golden"))

    def _check(name: str, actual: str) -> None:
        check_golden(name, actual, regenerate=regenerate)

    return _check


@pytest.fixture
def golden_json(golden: Callable[[str, str], None]) -> Callable[[str, Any], None]:
    """``golden_json(name, document)`` compares the stable JSON form of a document."""

    def _check(name: str, document: Any) -> None:
        golden(name, render_json(document))

    return _check


@pytest.fixture
def tracker() -> Iterator[Tracker]:
    """Ends, at teardown, exactly the fake processes a test recorded (by pid and start time)."""
    t = Tracker()
    yield t
    t.cleanup()


@pytest.fixture
def forgectl(tmp_path: Path, tracker: Tracker) -> Iterator[FakeForgectl]:
    """The fake forgectl of one test (``tests/fakebin/forgectl``): script what the host answers,
    put ``environ()`` in the code under test and read ``calls()``. When the test ends, a fake error,
    an argv real forgectl would refuse, or a scripted step nobody asked for fails it."""
    fake = FakeForgectl(tmp_path / "forgectl-fake", tracker)
    yield fake
    fake.assert_clean()
