"""Dedupe keys and the plan hash (docs/interface.md: Dedupe keys, Posting plan; issue #21).

Keys come from the inputs, never from the rendered text, so a retry under a new config, decision
id or tool version reuses them. A content key is ``prc:<action>:<h>`` with ``h`` the first 20 hex
of the sha256 of the compact JSON array of its inputs (``ensure_ascii=False``, UTF-8). A JSON
array is its own length prefix: no choice of field values can move a boundary, and the action name
is the second element of every array, so the actions are domain separated.

Decisions: ``body`` is Text pipeline step 1's output (line separators become LF) and is otherwise
taken as given: no Unicode normalization, no whitespace trimming. A lone surrogate cannot be
encoded; every function raises ``ValueError`` for it (the message never holds the value). Callers
refuse such text earlier (``textpipe.text_problem``), so this is a backstop.

``plan_sha256`` hashes exactly the fields the spec lists. It takes mappings and copies out only the
known fields, so a plan output's extras (``bytes``, ``commands``) and key order never matter. The
spec's hash has no domain-separation prefix and none is added; the ``action`` field plays that
role. The text hashed is the final body as handed to forgectl (after step 6), without forgectl's
dedupe line. ``--override-escalation`` and the config are not inputs; they matter only through the
text and ids they change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from pr_comment import textpipe
from pr_comment.grammars import (
    MAX_DECISION_IDS,
    GrammarError,
    check_actor,
    check_branch,
    check_decision_id,
    check_dedupe_key,
    check_pr,
    check_reply_target,
    check_sha,
)

_DRAFT_ID_RE: Final = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_FINDING_ID_RE: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
_THREAD_RE: Final = re.compile(r"gh:rt:[A-Za-z0-9_=-]{1,100}|gl:d:[0-9a-f]{40}")
_ACTIONS: Final = ("review", "approve", "comment", "reply")
_EVENTS: Final = ("comment", "request_changes", "approve")
_SIDES: Final = ("right", "left")


def _str(value: object, what: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{what} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{what} holds a lone surrogate") from None
    return value


def _checked(check: Any, value: object, what: str) -> str:
    text = _str(value, what)
    try:
        return str(check(text))
    except GrammarError as err:
        raise ValueError(f"{what}: {err}") from None


def _pr_id(value: object) -> str:
    text = _str(value, "pr_id")
    if not text:
        raise ValueError("pr_id must not be empty")
    return text


def _body(value: object) -> str:
    return textpipe.normalize_lines(_str(value, "body"))


def _content_key(action: str, array: list[str]) -> str:
    raw = json.dumps(array, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"prc:{action}:{hashlib.sha256(raw).hexdigest()[:20]}"


def _draft_id(value: object) -> str:
    text = _str(value, "draft_id")
    if not _DRAFT_ID_RE.fullmatch(text):
        raise ValueError("draft_id must be 26 characters of Crockford base32")
    return text


def review_key(draft_id: str) -> str:
    """``prc:rv:<draft_id>``: no anchor, event or text, so re-anchoring or editing a posted draft
    reports ``already_posted`` instead of posting again."""
    return f"prc:rv:{_draft_id(draft_id)}"


def review_approve_key(draft_id: str, head_sha: str) -> str:
    """``prc:ra:<draft_id>:<first 12 hex of the pinned head>`` for ``review --approve``."""
    head = _checked(check_sha, head_sha, "head_sha")
    return f"prc:ra:{_draft_id(draft_id)}:{head[:12]}"


def comment_key(pr_id: str, body: str) -> str:
    return _content_key("comment", [_pr_id(pr_id), "comment", _body(body)])


def reply_key(pr_id: str, to: str, body: str) -> str:
    return _content_key(
        "reply",
        [_pr_id(pr_id), "reply", _checked(check_reply_target, to, "to"), _body(body)],
    )


def approve_key(pr_id: str, head_sha: str, base_ref: str, body: str | None) -> str:
    """``body`` is None (or empty) for an approve without ``--body-file``."""
    return _content_key(
        "approve",
        [
            _pr_id(pr_id),
            "approve",
            _checked(check_sha, head_sha, "head_sha"),
            _checked(check_branch, base_ref, "base_ref"),
            "" if body is None else _body(body),
        ],
    )


def _line(value: object, what: str, *, nullable: bool) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or value < 1:
        raise ValueError(f"{what} must be an integer of at least 1")
    return value


def _mapping(value: object, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be a mapping")
    return value


def _sequence(value: object, what: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{what} must be a list")
    return value


def _field(item: Mapping[str, Any], name: str, what: str) -> Any:
    if name not in item:
        raise ValueError(f"{what} is missing {name}")
    return item[name]


def plan_sha256(
    *,
    pr: str,
    action: str,
    event: str | None,
    head_sha: str,
    base_ref: str,
    dedupe_key: str,
    to: str | None,
    actor: str,
    decision_ids: Sequence[str],
    bodies: Sequence[Mapping[str, Any]],
    inline_comments: Sequence[Mapping[str, Any]],
    follow_ups: Sequence[Mapping[str, Any]],
) -> str:
    """sha256 (hex) of the plan's canonical JSON. Raises ``ValueError`` for a malformed plan."""
    if action not in _ACTIONS:
        raise ValueError("action must be review, approve, comment or reply")
    if event is not None and event not in _EVENTS:
        raise ValueError("event must be null, comment, request_changes or approve")
    ids = _sequence(decision_ids, "decision_ids")
    if len(ids) > MAX_DECISION_IDS:
        raise ValueError("too many decision ids")

    body_items: list[dict[str, str]] = []
    for raw in _sequence(bodies, "bodies"):
        item = _mapping(raw, "a bodies entry")
        body_items.append(
            {
                "role": _str(_field(item, "role", "a bodies entry"), "role"),
                "text": _str(_field(item, "text", "a bodies entry"), "text"),
            }
        )
    roles = [b["role"] for b in body_items]
    if not roles or roles[0] != "body" or len(set(roles)) != len(roles):
        raise ValueError("bodies must start with the body role and hold unique roles")
    for role in roles[1:]:
        if not role.startswith("inline:") or not _FINDING_ID_RE.fullmatch(role[7:]):
            raise ValueError("a bodies role must be body or inline:<finding id>")

    inline_items: list[dict[str, Any]] = []
    for raw in _sequence(inline_comments, "inline_comments"):
        item = _mapping(raw, "an inline_comments entry")
        what = "an inline_comments entry"
        finding_id = _str(_field(item, "finding_id", what), "finding_id")
        if not _FINDING_ID_RE.fullmatch(finding_id):
            raise ValueError("finding_id does not match its grammar")
        role = _str(_field(item, "role", what), "role")
        if role != f"inline:{finding_id}" or role not in roles:
            raise ValueError("an inline comment's role must name its finding and a bodies entry")
        path = _str(_field(item, "path", what), "path")
        if not path:
            raise ValueError("path must not be empty")
        side = _field(item, "side", what)
        if side not in _SIDES:
            raise ValueError("side must be right or left")
        inline_items.append(
            {
                "finding_id": finding_id,
                "path": path,
                "line": _line(_field(item, "line", what), "line", nullable=False),
                "start_line": _line(_field(item, "start_line", what), "start_line", nullable=True),
                "side": side,
                "role": role,
            }
        )

    follow_items: list[dict[str, str | None]] = []
    for raw in _sequence(follow_ups, "follow_ups"):
        item = _mapping(raw, "a follow_ups entry")
        if _field(item, "action", "a follow_ups entry") != "resolve":
            raise ValueError("a follow-up action must be resolve")
        thread = _field(item, "thread_id", "a follow_ups entry")
        if thread is not None and not (type(thread) is str and _THREAD_RE.fullmatch(thread)):
            raise ValueError("a follow-up thread_id must be a thread id or null")
        follow_items.append({"action": "resolve", "thread_id": thread})

    plan = {
        "pr": _checked(check_pr, pr, "pr"),
        "action": action,
        "event": event,
        "head_sha": _checked(check_sha, head_sha, "head_sha"),
        "base_ref": _checked(check_branch, base_ref, "base_ref"),
        "dedupe_key": _checked(check_dedupe_key, dedupe_key, "dedupe_key"),
        "to": None if to is None else _checked(check_reply_target, to, "to"),
        "actor": _checked(check_actor, actor, "actor"),
        "decision_id": [_checked(check_decision_id, d, "decision_id") for d in ids],
        "bodies": body_items,
        "inline_comments": inline_items,
        "follow_ups": follow_items,
    }
    raw_json = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
