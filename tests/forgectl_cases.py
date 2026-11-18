"""Reading the recorded forgectl cases (``forgectl_cases.json``, ``forgectl_stdin_cases.json``).

A case is an argv and an stdin that end before any host is read, with the exact exit code, stdout
and stderr the real forgectl (the pinned release) gave when it was recorded, or ``reaches_host``
when forgectl accepted the call and went on to ask a host (which, with no ``gh`` or ``glab``,
failed with ``tool_missing``). Two suites replay them: the fake must reproduce each one, and a
``live`` run replays them against the real binary.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent
CASES_FILE = TESTS / "forgectl_cases.json"
STDIN_CASES_FILE = TESTS / "forgectl_stdin_cases.json"


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stdin_bytes(spec: dict[str, Any]) -> bytes:
    """The bytes of a case's stdin: ``text`` (UTF-8, lone surrogates kept as their three-byte
    form), ``b64``, or ``parts`` (strings and ``{"repeat": [unit, count]}``, concatenated)."""
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


def python_neutral(text: str) -> str:
    """argparse quotes the choices of an invalid command from Python 3.13 on, not before."""
    return _CHOICES.sub("(choose from ...)", text)


def observed_of(exit_code: int | None, stdout: bytes, stderr: bytes) -> dict[str, Any]:
    """What a run left, in the form of a case's ``expect``."""
    text = stdout.decode("utf-8")
    try:
        document: Any = json.loads(text) if text.strip() else None
    except ValueError:
        document = None
    return json.loads(
        python_neutral(
            json.dumps(
                {
                    "exit": exit_code,
                    "stdout": document if document is not None else text,
                    "stderr": stderr.decode("utf-8"),
                }
            )
        )
    )


def expected_of(case: dict[str, Any]) -> dict[str, Any]:
    return json.loads(python_neutral(json.dumps(case["expect"])))


def error_code(stdout: bytes) -> str:
    return json.loads(stdout)["error"]["code"]
