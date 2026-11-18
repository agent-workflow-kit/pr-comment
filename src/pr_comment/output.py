"""Result objects and output (docs/interface.md: Output, Human output).

A handler returns an ``Outcome``: the exit code, the ``--json`` document and optional human
lines. ``render`` turns it into stdout and stderr text before anything is written, so a bad
document cannot leave half an output. With ``--json`` stdout carries exactly the document
(the result on exit 0 and for the commands that print their result document on other exits, the
error envelope otherwise); human output goes to stderr only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import EXIT_CODES, build_error, exit_code_for, sanitize

PROG = "pr-comment"

LOG = logging.getLogger("pr_comment")


@dataclass(frozen=True)
class Outcome:
    """What a handler returns.

    ``document`` is the ``--json`` document (None prints nothing). ``human`` holds extra stderr
    lines for a human run; ``--quiet`` drops them on success. ``print_document`` makes a
    successful run print the document on stdout without ``--json`` as well.

    ``stream`` is for a command whose stdout is too large to build first (``export``): it is
    called with a ``write(text)`` function after the other output, only on exit 0 and with no
    document. Its text goes to stdout as given, so it must be safe to print (ASCII).

    ``close`` releases what ``stream`` holds (an open file); the caller runs it when the stream
    never runs because something failed first. It must be safe to call more than once.
    """

    exit_code: int
    document: dict[str, Any] | None = None
    human: tuple[str, ...] = ()
    stream: Callable[[Callable[[str], None]], None] | None = None
    close: Callable[[], None] | None = None
    # True for a command whose result is its stdout product even without --json (draft new).
    print_document: bool = False
    # Lines that say why a result document (no top-level ``error``) has a non-zero exit: shown
    # even with ``--quiet``, like an error's one-line message (``resolve`` and ``unresolve``).
    human_errors: tuple[str, ...] = ()


def error_outcome(code: str, message: str, **fields: Any) -> Outcome:
    """An ``Outcome`` carrying an error envelope; the exit code follows from ``code``.

    ``fields`` are ``errors.build_error``'s keyword arguments.
    """
    return Outcome(exit_code_for(code), build_error(code, message, **fields))


def internal_outcome(exc: BaseException) -> Outcome:
    """The fixed exit-1 ``internal`` outcome for an uncaught exception, logged at ``error``.

    The envelope names only the exception type: the exception text may hold untrusted data.
    """
    LOG.error("unexpected %s", type(exc).__name__, exc_info=exc)
    return error_outcome("internal", f"unexpected {type(exc).__name__}")


def _check_error_document(outcome: Outcome) -> None:
    document = outcome.document
    if document is None:
        if outcome.exit_code != 0:
            raise TypeError("handler exit code is not 0 but it has no document")
        return
    if "error" not in document:
        return  # a result document on a non-zero exit (resolve, unresolve, draft check)
    error: object = document["error"]
    if outcome.exit_code == 0:
        raise TypeError("handler exit code is 0 but its document is an error")
    if (
        not isinstance(error, dict)
        or not isinstance(error.get("message"), str)  # pyright: ignore[reportUnknownMemberType]
        or not isinstance(error.get("code"), str)  # pyright: ignore[reportUnknownMemberType]
    ):
        raise TypeError("handler error document needs an error with a string code and message")
    try:
        expected = exit_code_for(error["code"])
    except ValueError as exc:
        raise TypeError(str(exc)) from None
    if expected != outcome.exit_code:
        raise TypeError(
            f"handler exit code {outcome.exit_code} does not match code {error['code']}"
        )


def check_outcome(result: object) -> Outcome:
    """Return ``result`` when it is a well-formed ``Outcome``; raise ``TypeError`` otherwise.

    The check includes that the document serializes (so ``render`` cannot fail half-way) and that
    an exit code agrees with the document: exit 0 never carries an error envelope, a non-zero
    exit carries a document, and an envelope's code maps to the exit code.
    """
    if not isinstance(result, Outcome):
        raise TypeError("handler did not return an Outcome")
    if type(result.exit_code) is not int or result.exit_code not in EXIT_CODES:
        raise TypeError("handler exit code is not a defined exit code")
    if result.document is not None and not isinstance(result.document, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise TypeError("handler document is not a dict")
    if (
        not isinstance(result.human, tuple)  # pyright: ignore[reportUnnecessaryIsInstance]
        or not isinstance(result.human_errors, tuple)  # pyright: ignore[reportUnnecessaryIsInstance]
        or not all(isinstance(line, str) for line in (*result.human, *result.human_errors))  # pyright: ignore[reportUnnecessaryIsInstance]
    ):
        raise TypeError("handler human lines are not a tuple of strings")
    _check_error_document(result)
    if result.stream is not None and (result.exit_code != 0 or result.document is not None):
        raise TypeError("handler stream needs exit code 0 and no document")
    if result.document is not None:
        dumps_document(result.document)  # raises TypeError or ValueError for what JSON cannot hold
    return result


def dumps_document(document: Mapping[str, Any]) -> str:
    """The JSON text the printer writes for a document (ASCII only)."""
    return json.dumps(document, ensure_ascii=True, allow_nan=False)


def _error_of(document: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if document is None:
        return None
    error = document.get("error")
    return error if isinstance(error, dict) else None  # pyright: ignore[reportUnknownVariableType]


def _human_error(error: Mapping[str, Any], *, verbose: bool) -> list[str]:
    message = sanitize(str(error.get("message", error.get("code"))))
    lines = [f"{PROG}: {message}"]
    if verbose:
        parts = [f"code={error.get('code')}"]
        for key in ("reason", "landed", "retry", "retry_after"):
            if error.get(key) is not None:
                parts.append(f"{key}={error[key]}")
        lines.append(f"{PROG}: " + " ".join(sanitize(part) for part in parts))
    return lines


def render(
    outcome: Outcome, *, as_json: bool, quiet: bool = False, verbose: bool = False
) -> tuple[str, str]:
    """The stdout and stderr text of an outcome.

    With ``as_json``, stdout is the document on one line (ASCII only). Human lines on stderr:
    an error's one-line message always (``verbose`` adds code, reason, landed and retry), the
    outcome's own lines only without ``quiet``. ``quiet`` wins over ``verbose``.
    """
    stdout = ""
    if outcome.document is not None and (
        as_json or (outcome.print_document and outcome.exit_code == 0)
    ):
        stdout = dumps_document(outcome.document) + "\n"
    lines: list[str] = []
    error = _error_of(outcome.document)
    if outcome.exit_code != 0 and error is not None:
        lines.extend(_human_error(error, verbose=verbose and not quiet))
    if outcome.exit_code != 0:
        lines.extend(sanitize(line) for line in outcome.human_errors)
    if not quiet:
        lines.extend(sanitize(line) for line in outcome.human)
    return stdout, "".join(f"{line}\n" for line in lines)
