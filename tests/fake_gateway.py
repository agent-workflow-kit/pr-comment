"""An in-process fake of the gateway for unit tests of code that calls forgectl.

``FakeGateway`` checks every call with the real ``validate_call`` (so a caller that builds a bad
argv fails its test), records it, and answers from a script of ``ForgectlResult`` values or
functions.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pr_comment.gateway import ForgectlResult, validate_call

Reply = ForgectlResult | Callable[["Call"], ForgectlResult] | BaseException


@dataclass(frozen=True)
class Call:
    subcommand: str
    args: tuple[str, ...]
    stdin: bytes | None


def ok(subcommand: str, document: Any, *, write: bool = False) -> ForgectlResult:
    """A successful forgectl result printing ``document`` as JSON."""
    return ForgectlResult(
        command=subcommand, write=write, exit_code=0, stdout=json.dumps(document).encode()
    )


class FakeGateway:
    def __init__(
        self, replies: Sequence[Reply] = (), version: str | None = "forgectl 0.1.0"
    ) -> None:
        self._replies = list(replies)
        self._version = version
        self.calls: list[Call] = []

    def push(self, reply: Reply) -> None:
        self._replies.append(reply)

    def call(
        self, subcommand: str, args: Sequence[str] = (), *, stdin: bytes | None = None
    ) -> ForgectlResult:
        validate_call(subcommand, args, stdin)
        call = Call(subcommand, tuple(args), stdin)
        self.calls.append(call)
        if not self._replies:
            raise AssertionError(f"unexpected forgectl call: {subcommand} {list(args)}")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply(call) if callable(reply) else reply

    def lower_version(self) -> str | None:
        return self._version
