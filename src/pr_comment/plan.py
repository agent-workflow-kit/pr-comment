"""The posting plan (docs/interface.md: Posting plan; issue #27).

A plan is everything a posting command will hand to forgectl, built once from the already checked
inputs: the pinned head and base, the event, the exact bodies (Text pipeline steps 3 to 6), each
inline comment, the fate of every finding, the follow-up writes, the forgectl input and the real
command. ``plan_sha256`` (``keys.plan_sha256``) covers all of it, so what a human confirms, what
``--expect-plan`` binds and what the write orders post cannot differ: the write orders send
``Plan.input`` byte for byte.

Everything here is pure: the caller (``prepare.py``) does every read and every check against the
host. ``build_review_plan`` and ``build_text_plan`` take one ``Context`` (identity, config, flags).
Rendering is deterministic: the same inputs give the same bytes and the same hash. One
``MentionNeutralizer`` is used per plan, and the order of the calls is the order the posted text
shows names: the attribution line, the review body (the draft body, file-level findings, moved
findings), then the inline comments in draft order.

Counts (``Plan.counts``, the ``counts`` of schemas/defs.json) are taken over the untrusted parts as
the pipeline gave them (after step 1, before step 3). Open decision 1: ``counts.links`` counts 0
for ordinary and ``javascript:`` links, so it is not an upper bound. Open decision 2: an applicable
suggestion swallowed by an open body fence is inert and silent (not in ``suggestions_degraded``).
Open decision 3: a fullwidth ``＠`` passes unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from pr_comment import keys, textpipe
from pr_comment.assemble import (
    MARKER,
    Body,
    build_body,
    build_inline,
    render_attribution,
    render_inline_finding,
    render_text_part,
)
from pr_comment.config import RepoSettings
from pr_comment.forge import Show
from pr_comment.mentions import MentionNeutralizer
from pr_comment.neutralize import TextCounts, count_parts
from pr_comment.reanchor import Reanchored
from pr_comment.render import Anchor, MovedFinding, finding_parts, render_review_parts

SEVERITIES: Final = ("blocker", "major", "minor", "nit", "info")
MOVED_REASONS: Final = (
    "changed",
    "file_removed",
    "diff_unavailable",
    "base_changed",
    "invalid_position",
    "files_incomplete",
)
PATH_MARKER: Final = "<U+"
FORGECTL: Final = "forgectl"
ACTIONS: Final = ("review", "comment", "reply", "approve")


# --- the plan --------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanBody:
    """One body handed to forgectl: ``role`` is ``body`` or ``inline:<finding id>``; ``text`` is the
    final text (after step 6) without forgectl's dedupe line."""

    role: str
    text: str

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))


@dataclass(frozen=True)
class PlanInline:
    finding_id: str
    path: str
    line: int
    start_line: int | None
    side: str

    @property
    def role(self) -> str:
        return f"inline:{self.finding_id}"


@dataclass(frozen=True)
class FollowUp:
    """A forgectl write after the post (the GitLab body-thread resolve). ``thread_id`` is None
    while it is known only after the post."""

    action: str
    thread_id: str | None


@dataclass(frozen=True)
class Moved:
    id: str
    reason: str


@dataclass(frozen=True)
class Degraded:
    id: str
    reason: str


@dataclass(frozen=True)
class Plan:
    """The posting plan. ``input`` is exactly what the write orders put on forgectl's stdin."""

    pr: str
    pr_id: str
    repo: str
    backend: str
    action: str
    event: str | None
    head_sha: str
    base_ref: str
    dedupe_key: str
    to: str | None
    actor: str
    decision_ids: tuple[str, ...]
    bodies: tuple[PlanBody, ...]
    inline_comments: tuple[PlanInline, ...]
    follow_ups: tuple[FollowUp, ...]
    input: bytes
    plan_sha256: str
    # the fate of every finding (review only)
    draft_id: str | None = None
    anchor_head: str | None = None
    posted_inline: tuple[str, ...] = ()
    file_level: tuple[str, ...] = ()
    moved: tuple[Moved, ...] = ()
    suggestions_degraded: tuple[Degraded, ...] = ()
    # the report
    mentions_neutralized: tuple[str, ...] = ()
    counts: Mapping[str, Any] = field(default_factory=dict)
    neutralized_lines: int = 0
    markers_removed: int = 0
    override_forgectl: bool = False

    @property
    def subcommand(self) -> str:
        return self.action

    def _flags(self) -> tuple[list[str], list[str]]:
        """The arguments before and after the ``--yes`` or ``--dry-run`` slot, in the order of the
        orders in docs/interface.md."""
        head = f"--expected-head={self.head_sha}"
        base = f"--expected-base={self.base_ref}"
        key = f"--dedupe-key={self.dedupe_key}"
        before: list[str]
        if self.action == "review":
            before = [self.pr, "--file", "-", head, base, key]
        elif self.action == "approve":
            before = [self.pr, "--body-file", "-", head, base, key]
        elif self.action == "reply":
            before = [self.pr, f"--to={self.to}", "--body-file", "-", key]
        else:
            before = [self.pr, "--body-file", "-", key]
        after = [f"--actor={self.actor}", *(f"--decision-id={d}" for d in self.decision_ids)]
        return before, after

    def precheck_args(self) -> list[str]:
        """forgectl's arguments for the pre-check: the real call with ``--dry-run`` for ``--yes``
        (the gateway adds ``--json``); the input goes on stdin."""
        before, after = self._flags()
        return [*before, "--dry-run", *after]

    def command(self) -> list[str]:
        """The real forgectl call (docs/interface.md, Review order step 9): with ``--json`` and
        ``--yes``; the input goes on stdin. ``--override-escalation`` only with the scope
        ``forgectl`` (not part of the hash: it changes no posted byte)."""
        before, after = self._flags()
        tail = ["--override-escalation"] if self.override_forgectl else []
        return [FORGECTL, self.action, *before, "--json", "--yes", *after, *tail]

    def shown_bodies(self) -> tuple[PlanBody, ...]:
        """The bodies as forgectl will post them: each followed by its dedupe line, which forgectl
        appends (the dialog shows these; the hash does not cover the line, the key does)."""
        return tuple(
            PlanBody(b.role, with_dedupe_line(b.text, self.dedupe_key)) for b in self.bodies
        )

    def document(self) -> dict[str, Any]:
        """The ``plan`` object of a ``--dry-run`` result (``defs.plan``)."""
        return {
            "plan_sha256": self.plan_sha256,
            "bodies": [{"role": b.role, "text": b.text, "bytes": b.bytes} for b in self.bodies],
            "inline_comments": [
                {
                    "finding_id": c.finding_id,
                    "path": c.path,
                    "line": c.line,
                    "start_line": c.start_line,
                    "side": c.side,
                    "role": c.role,
                }
                for c in self.inline_comments
            ],
            "commands": [self.command()],
            "follow_ups": [{"action": f.action, "thread_id": f.thread_id} for f in self.follow_ups],
        }


def with_dedupe_line(text: str, key: str) -> str:
    """``text`` followed by forgectl's dedupe line, as forgectl appends it."""
    return text + ("" if text.endswith("\n") else "\n") + f"<!-- forgectl:dedupe:{key} -->"


# --- context ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    """What every plan shares: the PR as ``show`` read it, who acts, and the config in effect."""

    show: Show
    actor: str
    decision_ids: tuple[str, ...]
    settings: RepoSettings
    me_name: str
    tool_version: str
    allow_mentions: tuple[str, ...] = ()
    dedupe_key: str | None = None  # a caller's --dedupe-key
    override_forgectl: bool = False  # --override-escalation=forgectl (never hashed)

    def mentions(self) -> MentionNeutralizer:
        return MentionNeutralizer(
            self.settings.mentions_policy, (*self.settings.mentions_allow, *self.allow_mentions)
        )

    def attribution(self, mentions: MentionNeutralizer, head_sha: str) -> str | None:
        """The attribution line; call it before any part is rendered."""
        return render_attribution(
            self.actor,
            footer_human=self.settings.footer_human,
            footer_auto=self.settings.footer_auto,
            me_name=self.me_name,
            head_sha=head_sha,
            decision_id=self.decision_ids[0] if self.decision_ids else None,
            tool_version=self.tool_version,
            mentions=mentions,
        )


# --- counts ----------------------------------------------------------------------------------


def _empty_counts() -> dict[str, Any]:
    return {
        "inline": 0,
        "moved": dict.fromkeys(MOVED_REASONS, 0),
        "file_level": 0,
        "severity": dict.fromkeys(SEVERITIES, 0),
        "suggestions_degraded": 0,
        "mentions_neutralized": 0,
        "links": 0,
        "images": 0,
        "raw_html": 0,
        "invisible": 0,
    }


def _with_text_counts(counts: dict[str, Any], text: TextCounts, mentions: int) -> dict[str, Any]:
    counts.update(
        links=text.links,
        images=text.images,
        raw_html=text.raw_html,
        invisible=text.invisible,
        mentions_neutralized=mentions,
    )
    return counts


# --- forgectl input and the hash -------------------------------------------------------------


def review_input(event: str, body: str, comments: Sequence[tuple[PlanInline, str]]) -> bytes:
    """forgectl's review input (schemas/review-input.json): ``{event, body, comments}``. One
    serialization: fixed key order, ASCII-only JSON, no spaces. ``start_line`` only when set."""
    items: list[dict[str, Any]] = []
    for inline, text in comments:
        item: dict[str, Any] = {
            "path": inline.path,
            "line": inline.line,
            "side": inline.side,
        }
        if inline.start_line is not None:
            item["start_line"] = inline.start_line
        item["body"] = text
        items.append(item)
    document = {"event": event, "body": body, "comments": items}
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _hash(
    ctx: Context,
    *,
    action: str,
    event: str | None,
    head_sha: str,
    base_ref: str,
    key: str,
    to: str | None,
    bodies: Sequence[PlanBody],
    inline: Sequence[PlanInline],
    follow_ups: Sequence[FollowUp],
) -> str:
    return keys.plan_sha256(
        pr=ctx.show.pr,
        action=action,
        event=event,
        head_sha=head_sha,
        base_ref=base_ref,
        dedupe_key=key,
        to=to,
        actor=ctx.actor,
        decision_ids=ctx.decision_ids,
        bodies=[{"role": b.role, "text": b.text} for b in bodies],
        inline_comments=[
            {
                "finding_id": c.finding_id,
                "path": c.path,
                "line": c.line,
                "start_line": c.start_line,
                "side": c.side,
                "role": c.role,
            }
            for c in inline
        ],
        follow_ups=[{"action": f.action, "thread_id": f.thread_id} for f in follow_ups],
    )


# --- review ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """A finding that moved to the review body in this run (re-anchoring), as it was in the source
    draft, with the last anchor at which it had a known line and its place in the source draft."""

    finding: Mapping[str, Any]
    reason: str
    anchor: Anchor
    order: int


@dataclass(frozen=True)
class ReviewSource:
    """The review's content after re-anchoring and the left-side check.

    ``kept`` is ``reanchored_draft.findings`` (inline and file-level findings, draft order);
    ``moves`` is this run's moves; ``unanchored`` is the source draft's own ``unanchored`` entries;
    ``order`` maps a finding id to its place in the source draft's ``findings`` (moves of this run
    keep draft order across re-anchoring and pre-check moves). ``head_sha`` and ``base_ref`` are
    the pinned values; ``anchor_head`` is the draft's own head (A).
    """

    draft_id: str
    body: str
    event: str
    approve: bool
    head_sha: str
    base_ref: str
    anchor_head: str
    kept: Sequence[Mapping[str, Any]]
    moves: Sequence[Move]
    unanchored: Sequence[Mapping[str, Any]]
    order: Mapping[str, int]


def review_source(
    data: Mapping[str, Any], done: Reanchored, *, head_sha: str, approve: bool
) -> ReviewSource:
    """The source of a plan from the loaded draft's data and its ``reanchor`` result (which is
    run for every review, so the left-side check applies with or without ``--reanchor-to``).
    ``head_sha`` is the pinned head; the pinned base is the draft's own ``anchor.base_ref``."""
    anchor: Mapping[str, Any] = data["anchor"]
    order = {str(f["id"]): i for i, f in enumerate(data["findings"])}
    moves = [
        Move(
            m.finding,
            m.reason,
            Anchor(anchor["head_sha"], m.finding.get("line"), m.finding.get("start_line")),
            order[str(m.finding["id"])],
        )
        for m in done.moved
    ]
    return ReviewSource(
        draft_id=data["draft_id"],
        body=data["body"],
        event=data["event"],
        approve=approve,
        head_sha=head_sha,
        base_ref=anchor["base_ref"],
        anchor_head=anchor["head_sha"],
        kept=done.draft["findings"],
        moves=moves,
        unanchored=data.get("unanchored") or [],
        order=order,
    )


def _is_inline(finding: Mapping[str, Any]) -> bool:
    return finding.get("line") is not None


def _anchor_of(finding: Mapping[str, Any], sha: str) -> Anchor:
    return Anchor(sha, finding.get("line"), finding.get("start_line"))


def _follow_ups(ctx: Context, event: str) -> tuple[FollowUp, ...]:
    """The GitLab body-thread resolve (docs/interface.md, GitLab review body thread): after a
    ``comment`` or approving review with ``non_blocking``, also after ``request_changes`` with
    ``always``. Its thread is known only after the post."""
    if ctx.show.backend != "gitlab":
        return ()
    setting = ctx.settings.resolve_body_thread
    applies = setting == "always" or (setting == "non_blocking" and event != "request_changes")
    return (FollowUp("resolve", None),) if applies else ()


def build_review_plan(
    ctx: Context, source: ReviewSource, extra_moves: Mapping[str, str] | None = None
) -> Plan:
    """The plan of ``review``. ``extra_moves`` maps the id of an inline finding to the reason it
    moves to the review body (the pre-check rounds: ``invalid_position``, ``files_incomplete``);
    a path holding ``<U+`` moves here too, as ``invalid_position`` (Position validation).

    Raises ``ValueError`` for an id that is not an inline finding, and ``ConfigError`` for an
    attribution line that does not start with a letter.
    """
    extra = dict(extra_moves or {})
    show = ctx.show
    event = "approve" if source.approve else source.event

    inline_findings: list[Mapping[str, Any]] = []
    file_level: list[Mapping[str, Any]] = []
    moves: list[Move] = list(source.moves)
    kept_ids = {str(f["id"]) for f in source.kept if _is_inline(f)}
    unknown = sorted(set(extra) - kept_ids)
    if unknown:
        raise ValueError("a finding to move is not an inline finding of the draft")
    for finding in source.kept:
        fid = str(finding["id"])
        if not _is_inline(finding):
            file_level.append(finding)
            continue
        reason = extra.get(fid)
        if reason is None and PATH_MARKER in str(finding["path"]):
            reason = "invalid_position"
        if reason is None:
            inline_findings.append(finding)
            continue
        if reason not in MOVED_REASONS:
            raise ValueError("unknown moved reason")
        moves.append(Move(finding, reason, _anchor_of(finding, source.head_sha), source.order[fid]))
    moves.sort(key=lambda m: m.order)

    mentions = ctx.mentions()
    attribution = ctx.attribution(mentions, source.head_sha)
    entries = [MovedFinding(m.finding, m.reason, m.anchor) for m in moves]
    entries += [
        MovedFinding(
            e["finding"],
            str(e["reason"]),
            _anchor_of(e["finding"], str(e["head_sha"])),
        )
        for e in source.unanchored
    ]
    body_text = textpipe.normalize_lines(source.body) if source.body else ""
    parts = render_review_parts(body_text, file_level, entries, mentions)
    review_body = build_body(parts, attribution)

    inline: list[PlanInline] = []
    bodies: list[PlanBody] = [PlanBody("body", review_body.text)]
    degraded: list[Degraded] = []
    neutralized = review_body.neutralized_lines
    removed = review_body.markers_removed
    texts: list[tuple[PlanInline, str]] = []
    for finding in inline_findings:
        rendered = render_inline_finding(finding, mentions, host=show.backend)
        posted: Body = build_inline(rendered.text)
        spot = PlanInline(
            str(finding["id"]),
            str(finding["path"]),
            int(finding["line"]),
            finding.get("start_line"),
            str(finding.get("side") or "right"),
        )
        inline.append(spot)
        bodies.append(PlanBody(spot.role, posted.text))
        texts.append((spot, posted.text))
        neutralized += posted.neutralized_lines
        removed += posted.markers_removed
        if rendered.degraded is not None:
            degraded.append(Degraded(spot.finding_id, rendered.degraded))

    counts = _review_counts(
        source, body_text, inline_findings, file_level, moves, degraded, mentions
    )
    key = ctx.dedupe_key or (
        keys.review_approve_key(source.draft_id, source.head_sha)
        if source.approve
        else keys.review_key(source.draft_id)
    )
    follow_ups = _follow_ups(ctx, event)
    digest = _hash(
        ctx,
        action="review",
        event=event,
        head_sha=source.head_sha,
        base_ref=source.base_ref,
        key=key,
        to=None,
        bodies=bodies,
        inline=inline,
        follow_ups=follow_ups,
    )
    return Plan(
        pr=show.pr,
        pr_id=show.pr_id,
        repo=show.repo,
        backend=show.backend,
        action="review",
        event=event,
        head_sha=source.head_sha,
        base_ref=source.base_ref,
        dedupe_key=key,
        to=None,
        actor=ctx.actor,
        decision_ids=ctx.decision_ids,
        bodies=tuple(bodies),
        inline_comments=tuple(inline),
        follow_ups=follow_ups,
        input=review_input(event, review_body.text, texts),
        plan_sha256=digest,
        draft_id=source.draft_id,
        anchor_head=source.anchor_head,
        posted_inline=tuple(c.finding_id for c in inline),
        file_level=tuple(str(f["id"]) for f in file_level),
        moved=tuple(Moved(str(m.finding["id"]), m.reason) for m in moves),
        suggestions_degraded=tuple(degraded),
        mentions_neutralized=mentions.names,
        counts=counts,
        neutralized_lines=neutralized,
        markers_removed=removed,
        override_forgectl=ctx.override_forgectl,
    )


def _review_counts(
    source: ReviewSource,
    body_text: str,
    inline: Sequence[Mapping[str, Any]],
    file_level: Sequence[Mapping[str, Any]],
    moves: Sequence[Move],
    degraded: Sequence[Degraded],
    mentions: MentionNeutralizer,
) -> dict[str, Any]:
    """Counts over the raw untrusted parts (``count_parts(finding_parts(f))`` and the draft body),
    taken before the pipeline neutralizes anything; severity over the findings and ``unanchored``
    entries; moved by reason over this run's moves."""
    counts = _empty_counts()
    shown: list[Mapping[str, Any]] = [*inline, *file_level, *(m.finding for m in moves)]
    shown += [e["finding"] for e in source.unanchored]
    parts: list[str] = [body_text] if body_text else []
    for finding in shown:
        parts.extend(finding_parts(finding))
        severity = finding.get("severity")
        if severity in SEVERITIES:
            counts["severity"][severity] += 1
    counts["inline"] = len(inline)
    counts["file_level"] = len(file_level)
    counts["suggestions_degraded"] = len(degraded)
    for m in moves:
        counts["moved"][m.reason] += 1
    return _with_text_counts(counts, count_parts(parts), mentions.count)


# --- comment, reply, approve -----------------------------------------------------------------


def build_text_plan(
    ctx: Context,
    *,
    action: str,
    text: str | None,
    head_sha: str,
    base_ref: str,
    to: str | None = None,
) -> Plan:
    """The plan of ``comment``, ``reply`` or ``approve``. ``text`` is the body file as Text
    pipeline step 1 left it (None for an approve without a body file: the attribution line and the
    marker only). ``head_sha`` and ``base_ref`` are the pinned values (the expected head, or
    ``show``'s). Always sends a body: forgectl's ``approve`` takes a dedupe key only with one.

    Raises ``ValueError`` for an unknown action, ``to`` that does not fit the action or text that
    the key refuses (a control character, a lone surrogate).
    """
    if action not in ("comment", "reply", "approve"):
        raise ValueError("action must be comment, reply or approve")
    if (to is not None) != (action == "reply"):
        raise ValueError("to is given exactly for reply")
    if text is None and action != "approve":
        raise ValueError("a comment and a reply need a body")
    show = ctx.show
    event = "approve" if action == "approve" else None
    mentions = ctx.mentions()
    attribution = ctx.attribution(mentions, head_sha)
    parts: list[str] = []
    raw_parts: list[str] = []
    if text is not None:
        normal = textpipe.normalize_lines(text)
        raw_parts.append(normal)
        parts.append(render_text_part(text, mentions))
    body = build_body(parts, attribution)
    if ctx.dedupe_key is not None:
        key = ctx.dedupe_key
    elif action == "comment":
        assert text is not None
        key = keys.comment_key(show.pr_id, text)
    elif action == "reply":
        assert text is not None and to is not None
        key = keys.reply_key(show.pr_id, to, text)
    else:
        key = keys.approve_key(show.pr_id, head_sha, base_ref, text)
    bodies = [PlanBody("body", body.text)]
    digest = _hash(
        ctx,
        action=action,
        event=event,
        head_sha=head_sha,
        base_ref=base_ref,
        key=key,
        to=to,
        bodies=bodies,
        inline=(),
        follow_ups=(),
    )
    counts = _with_text_counts(_empty_counts(), count_parts(raw_parts), mentions.count)
    return Plan(
        pr=show.pr,
        pr_id=show.pr_id,
        repo=show.repo,
        backend=show.backend,
        action=action,
        event=event,
        head_sha=head_sha,
        base_ref=base_ref,
        dedupe_key=key,
        to=to,
        actor=ctx.actor,
        decision_ids=ctx.decision_ids,
        bodies=tuple(bodies),
        inline_comments=(),
        follow_ups=(),
        input=body.text.encode("utf-8"),
        plan_sha256=digest,
        mentions_neutralized=mentions.names,
        counts=counts,
        neutralized_lines=body.neutralized_lines,
        markers_removed=body.markers_removed,
        override_forgectl=ctx.override_forgectl,
    )


def marker_present(plan: Plan) -> bool:
    """Every body ends with pr-comment's marker line (a self-check for tests and the write
    orders)."""
    return all(b.text.rstrip("\n").endswith(MARKER) for b in plan.bodies)


def role_of(plan: Plan, index: int | None) -> str:
    """The body role of forgectl's ``details.index`` for a body or inline comment: ``body`` for
    None, ``inline:<id>`` for an index into the plan's inline comments. Raises ``IndexError``."""
    if index is None:
        return "body"
    if type(index) is not int or not 0 <= index < len(plan.inline_comments):
        raise IndexError("index is outside the inline comments")
    return plan.inline_comments[index].role


def moved_ids(items: Iterable[Moved]) -> list[dict[str, str]]:
    return [{"id": m.id, "reason": m.reason} for m in items]
