"""A short deadline for the forgectl calls that can write (issue #94).

The ``short_write_deadline`` fixture (registered in ``conftest.py``) gives a test a callable that
installs the override; at teardown it fails the test if the override never matched a write call,
so a changed call path cannot turn the test into one that waits for the normal deadline or never
reaches the hang it means to cover.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest

ShortWriteDeadline = Callable[..., None]


@pytest.fixture
def short_write_deadline(monkeypatch: pytest.MonkeyPatch) -> Iterator[ShortWriteDeadline]:
    """Call it as ``short_write_deadline()`` (optionally with ``seconds``) at the start of a test.

    Only the forgectl calls that can write get the short deadline. The earlier ``show``, pre-check
    and ``--version`` calls keep the normal deadline, so a stall under load cannot turn into a
    ``network_error`` before the write is reached. The write's own deadline stays at least 5 s: a
    hung write is killed at it either way, and a loaded machine that is slow to start the fake
    still ends in the same outcome. The count of overridden calls is checked at teardown.
    """
    from pr_comment import gateway

    real = gateway.run_process
    installed = False
    overridden = 0

    def install(seconds: float = 6.0) -> None:
        nonlocal installed

        def run_process(*args: Any, **kwargs: Any) -> Any:
            nonlocal overridden
            if kwargs.get("write"):
                kwargs["deadline"] = seconds
                overridden += 1
            return real(*args, **kwargs)

        installed = True
        monkeypatch.setattr(gateway, "run_process", run_process)

    yield install
    if installed:
        assert overridden >= 1, (
            "short_write_deadline was installed but no write call went through "
            "gateway.run_process(write=True): the call path changed"
        )
