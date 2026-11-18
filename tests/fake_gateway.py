"""An in-process fake of the gateway for unit tests of code that calls forgectl.

``FakeGateway`` checks every call with the real ``validate_call`` (so a caller that builds a bad
argv fails its test), records it, and answers from a script of ``ForgectlResult`` values or
functions. ``validate_call`` decides whether the call can write: a scripted result is given that
``write`` flag, and a result that says otherwise fails the test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pr_comment.gateway import WRITE_SUBCOMMANDS, ForgectlResult, ToolMissing, validate_call
from tests import fake_forgectl

Reply = ForgectlResult | Callable[["Call"], ForgectlResult] | BaseException


@dataclass(frozen=True)
class Call:
    subcommand: str
    args: tuple[str, ...]
    stdin: bytes | None


def ok(subcommand: str, document: Any, *, write: bool | None = None) -> ForgectlResult:
    """A successful forgectl result printing ``document`` as JSON. ``write`` defaults to whether
    the subcommand writes; pass ``False`` for a ``--dry-run``."""
    if write is None:
        write = subcommand in WRITE_SUBCOMMANDS
    return ForgectlResult(
        command=subcommand, write=write, exit_code=0, stdout=json.dumps(document).encode()
    )


class FakeGateway:
    def __init__(
        self,
        replies: Sequence[Reply] = (),
        version: str | None = "forgectl 0.1.0",
        *,
        missing: ToolMissing | None = None,
    ) -> None:
        self._replies = list(replies)
        self._version = version
        self._missing = missing
        self.calls: list[Call] = []

    def push(self, reply: Reply) -> None:
        self._replies.append(reply)

    def call(
        self, subcommand: str, args: Sequence[str] = (), *, stdin: bytes | None = None
    ) -> ForgectlResult:
        write = validate_call(subcommand, args, stdin)
        call = Call(subcommand, tuple(args), stdin)
        self.calls.append(call)
        if not self._replies:
            raise AssertionError(f"unexpected forgectl call: {subcommand} {list(args)}")
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        result = reply(call) if callable(reply) else reply
        if result.write != write:
            raise AssertionError(
                f"scripted reply for {subcommand} {list(args)} has write={result.write}, "
                f"but the call has write={write}"
            )
        return replace(result, write=write)

    def lower_version(self) -> str | None:
        return self._version

    def resolve(self) -> str:
        if self._missing is not None:
            raise self._missing
        return "/fake/forgectl"


class PinnedGateway(FakeGateway):
    """A ``FakeGateway`` held to forgectl itself, not only to pr-comment's own call rules.

    Every call must be one the pinned forgectl accepts (its argument grammar, the closed host
    list, the flags it requires, the input it reads from stdin), and every scripted result one it
    could print (its schemas, its exit codes and error envelopes, ``would_refuse`` on a dry run).
    A call or a result forgectl would refuse fails the test, which ``FakeGateway`` lets through.

    Use it for tests of code that calls forgectl. ``FakeGateway`` stays for tests of ``lower.py``
    that must feed it a document forgectl never prints (a contract mismatch).
    """

    def __init__(
        self,
        replies: Sequence[Reply] = (),
        version: str | None = None,
        *,
        missing: ToolMissing | None = None,
        hosts: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            replies,
            version=f"forgectl {fake_forgectl.VERSION}" if version is None else version,
            missing=missing,
        )
        self.hosts = hosts or {}

    def call(
        self, subcommand: str, args: Sequence[str] = (), *, stdin: bytes | None = None
    ) -> ForgectlResult:
        argv = [subcommand, "--json", "--timeout=60s", *args]
        # a call forgectl refuses is a bug of the caller, whatever the answer would have been
        refusal = fake_forgectl.check_call(argv, stdin, hosts=self.hosts)
        if refusal is not None:
            raise AssertionError(refusal)
        result = super().call(subcommand, args, stdin=stdin)
        problems = fake_forgectl.judge(
            argv, stdin, result.exit_code, result.stdout, hosts=self.hosts
        )
        if problems:
            raise AssertionError(
                f"forgectl could not have answered {subcommand} {list(args)} this way: "
                + "; ".join(problems)
            )
        return result
