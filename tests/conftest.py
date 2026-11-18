from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import pytest

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
