"""``pr-comment validate``: an offline, read-only check of the config and of forgectl.

The handler loads the config itself (``cli._load_config_into`` skips this command, because it
writes ``config_warning`` lines to log.jsonl and this command writes nothing), resolves forgectl
and reads ``forgectl --version`` through the gateway. It opens no state directory, runs no other
forgectl command and reads no stdin. See docs/interface.md, ``validate``.
"""

from __future__ import annotations

import argparse
import os
import re
import unicodedata
from typing import Any, Final

from .config import ConfigError, LoadedConfig, load_config
from .errors import sanitize
from .gateway import MAX_VERSION_CHARS, Gateway, ProcessGateway, ToolMissing
from .lower import mismatch_outcome, tool_missing_outcome
from .output import Outcome, error_outcome

#: The lowest forgectl release of the supported range (interface.md, forgectl edge: 0.55.x).
MINIMUM_FORGECTL: Final = (0, 55, 0)
#: Versions from here on are newer than the supported range: a warning, never a refusal.
UNTESTED_FROM: Final = (0, 56, 0)

_VERSION_RE: Final = re.compile(
    r"forgectl ([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})(?:[-+][0-9A-Za-z.+-]*)?"
)


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in version)


def parse_forgectl_version(line: str | None) -> tuple[int, int, int] | None:
    """The three numbers of ``forgectl X.Y.Z[-pre][+build]``; None for any other text."""
    if line is None:
        return None
    match = _VERSION_RE.fullmatch(line)
    if match is None:
        return None
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


#: The longest first line accepted: the gateway's display cut, so nothing is hidden by it.
_MAX_LINE_CHARS: Final = MAX_VERSION_CHARS


def version_line(raw: bytes | None) -> str | None:
    """The first line of the raw ``--version`` output (split on a newline only), or None when it
    holds a control or line-separator character (VT, FF, FS, GS, RS, NEL, U+2028/9, ESC, NUL, CR,
    a tab...) or is longer than 64 characters: such a line is not the documented one."""
    line = (raw or b"").decode("utf-8", "replace").split("\n", 1)[0]
    if len(line) > _MAX_LINE_CHARS:
        return None
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in line):
        return None
    return line


def check(loaded: LoadedConfig, gateway: Gateway) -> Outcome:
    """The result of the checks after the config loaded: forgectl, then the warnings."""
    try:
        gateway.resolve()
    except ToolMissing as exc:
        return tool_missing_outcome(exc)
    raw = gateway.version_output()
    if not raw:
        return mismatch_outcome(
            "--version", "forgectl --version failed or timed out", gateway=gateway
        )
    version = parse_forgectl_version(version_line(raw))
    if version is None:
        first_line = raw.decode("utf-8", "replace").split("\n", 1)[0].strip()
        return mismatch_outcome(
            "--version", "unreadable version", gateway=gateway, version_text=first_line
        )
    if version < MINIMUM_FORGECTL:
        return mismatch_outcome(
            "--version", f"older than {_dotted(MINIMUM_FORGECTL)}", gateway=gateway
        )
    warnings: list[str] = []
    human = [
        f"config ok ({loaded.config_path or 'no config file'})",
        f"forgectl {_dotted(version)} (minimum {_dotted(MINIMUM_FORGECTL)})",
    ]
    if loaded.warnings:
        warnings.append("config_warning")
        human.extend(
            f"warning: {sanitize(w.file)} sets {', '.join(sanitize(k) for k in w.keys)}"
            for w in loaded.warnings
        )
    if version >= UNTESTED_FROM:
        warnings.append("forgectl_untested_version")
        human.append(
            "warning: forgectl is newer than the supported range "
            f"({MINIMUM_FORGECTL[0]}.{MINIMUM_FORGECTL[1]}.x)"
        )
    document: dict[str, Any] = {
        "valid": True,
        "config_path": loaded.config_path,
        "forgectl": {"version": _dotted(version), "minimum": _dotted(MINIMUM_FORGECTL)},
        "warnings": warnings,
    }
    return Outcome(0, document, tuple(human))


def handle_validate(args: argparse.Namespace) -> Outcome:
    """The ``validate`` handler; reads ``args.config``."""
    try:
        loaded = load_config(args.config, env=os.environ)
    except ConfigError as exc:
        return error_outcome(
            "config_invalid", sanitize(str(exc)), details={"key": sanitize(exc.key)}
        )
    return check(loaded, ProcessGateway.from_config(loaded, environ=os.environ))
