"""The live scenarios. Each one is a function of a ``Ctx`` and works in every mode: in ``plan`` mode
it only records its steps, in ``preflight`` mode its writes are dry runs, in ``run`` mode it posts
to the throwaway target and checks the host's answers. ``rows`` names the checklist rows
(``checklist.py``) a scenario settles; a result the suite cannot decide is recorded with
``ctx.observe`` for design.md's Live verification table instead of being asserted.

Open user decisions are marked ``OPEN DECISION`` where a result bears on them; the suite records the
host fact and decides nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import helpers
from .ctx import ZERO_SHA, Ctx
from .errors import LiveRefusal
from .settings import LiveSettings, Target

Fn = Callable[[Ctx], None]
GITHUB = ("github",)
GITLAB = ("gitlab",)
BOTH = ("github", "gitlab")


@dataclass(frozen=True)
class Scenario:
    id: str
    fn: Fn
    rows: tuple[str, ...]
    backends: tuple[str, ...]
    writes: bool
    needs: tuple[str, ...]  # old_head, closed_ref, approve, slow
    doc: str


SCENARIOS: dict[str, Scenario] = {}


def scenario(
    *,
    rows: tuple[str, ...],
    backends: tuple[str, ...] = BOTH,
    writes: bool = True,
    needs: tuple[str, ...] = (),
) -> Callable[[Fn], Fn]:
    def register(fn: Fn) -> Fn:
        name = fn.__name__.removeprefix("s_")
        doc = (fn.__doc__ or "").strip().split("\n")[0]
        SCENARIOS[name] = Scenario(name, fn, rows, backends, writes, needs, doc)
        return fn

    return register


def skip_reason(sc: Scenario, settings: LiveSettings, target: Target) -> str | None:
    """Why ``sc`` does not run on ``target`` (None: it does)."""
    if target.backend not in sc.backends:
        return f"{sc.id} is for {'/'.join(sc.backends)} only"
    if "old_head" in sc.needs and not target.old_head:
        return f"{sc.id} needs PR_COMMENT_LIVE_{target.key}_OLD_HEAD"
    if "closed_ref" in sc.needs and not target.closed_ref:
        return f"{sc.id} needs PR_COMMENT_LIVE_{target.key}_CLOSED_PR"
    if "approve" in sc.needs and not (settings.approve and target.approve_ref):
        return (
            f"{sc.id} needs PR_COMMENT_LIVE_APPROVE=1 and PR_COMMENT_LIVE_{target.key}_APPROVE_PR"
        )
    if "slow" in sc.needs and not settings.slow:
        return f"{sc.id} takes over a minute: set PR_COMMENT_LIVE_SLOW=1"
    return None


def pairs(settings: LiveSettings) -> list[tuple[Scenario, Target]]:
    """Every (scenario, target) the configured targets give, in registry order."""
    found = []
    for sc in SCENARIOS.values():
        for target in settings.targets.values():
            found.append((sc, target))
    return found


def _forgectl_hosts(ctx: Ctx) -> str:
    """The ``hosts`` block a scratch forgectl config needs for a non-built-in host."""
    if ctx.target and ctx.target.is_enterprise and ctx.target.host:
        return f"hosts:\n  {ctx.target.host}:\n    backend: {ctx.backend}\n"
    return ""


def _doc(out_doc: dict[str, Any] | None, *path: str) -> Any:
    cursor: Any = out_doc
    for key in path:
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(key)
    return cursor


# --- reads ---------------------------------------------------------------------------------------


@scenario(rows=("gw.version_format", "fk.whoami", "sc.versions", "bs.base_ref_forms"), writes=False)
def s_forgectl_reads(ctx: Ctx) -> None:
    """forgectl's version, whoami and show; the base ref form pr-comment copies into a draft."""
    version = ctx.runner.fg("version", "--version", config="", strict=False)
    text = version.stdout.strip()
    # recorded, not asserted: the format is what the row is there to find out
    ctx.observe("gw.version_format", "version_line", text)
    ctx.observe(
        "gw.version_format",
        "matches_forgectl_x_y_z",
        re.fullmatch(r"forgectl [0-9]+\.[0-9]+\.[0-9]+\S*", text) is not None,
    )
    who = ctx.runner.fg("whoami", "whoami", "--json")
    ctx.check(who.code in (0, 12), "whoami exits 0 or 12 (partial results)", "fk.whoami")
    if who.real and who.doc:
        hosts = [h.get("host") for h in who.doc.get("hosts") or []]
        ctx.observe("fk.whoami", "exit", who.code)
        ctx.observe("fk.whoami", "hosts", sorted(str(h) for h in hosts))
        ctx.observe("fk.whoami", "errors", len(who.doc.get("errors") or []))
        ctx.check(
            ctx.target is None or (ctx.target.host in hosts),
            "whoami lists the target's host (is a credential set up for it?)",
            "fk.whoami",
        )
    show = ctx.show()
    ctx.observe("bs.base_ref_forms", "base_ref", _doc(show, "base", "ref"))
    draft = ctx.draft("reads", [])
    check = ctx.runner.pc(
        "draft-check",
        "draft",
        "check",
        "-",
        stdin=json.dumps(draft).encode("utf-8"),
        write=False,
        actor=None,
    )
    ctx.want(check, 0, row="bs.base_ref_forms")


# --- comments, dedupe, bytes ---------------------------------------------------------------------


@scenario(rows=("gw.flag_forms", "gw.stdout_strict_json", "fk.id_prefixes", "sc.post_bytes"))
def s_comment_post(ctx: Ctx) -> None:
    """A comment: ids, the forms forwarded to forgectl, the marker and the author."""
    ctx.tap_since()
    out = ctx.comment("post", ctx.text("post", "A plain comment from the live suite."))
    ctx.want(out, 0)
    ids = ctx.ids(out)
    prefix = "gh:ic:" if ctx.github else "gl:n:"
    ctx.check(
        len(ids) == 1 and ids[0].startswith(prefix),
        f"one id with prefix {prefix}",
        "fk.id_prefixes",
    )
    ctx.check(_doc(out.doc, "dry_run") is False, "dry_run is false")
    if ctx.live:
        thread_ids = _doc(out.doc, "thread_ids")
        ctx.check(
            (thread_ids == [] or thread_ids is None) if ctx.github else True,
            "GitHub's comment has no thread ids",
            "fk.id_prefixes",
        )
        for argv in ctx.tap_since():
            problems = helpers.flag_form_problems(argv)
            ctx.check(
                not problems, f"forgectl got a flag in the wrong form: {problems}", "gw.flag_forms"
            )
        stored = ctx.stored_body("post")
        ctx.check(stored is not None, "the posted comment is on the PR", "sc.post_bytes")
        if stored is not None:
            ctx.check(
                helpers.has_marker(stored), "marker detection finds the marker", "sc.post_bytes"
            )


@scenario(rows=("fk.dedupe_hit", "sc.yes_dedupe", "gw.dry_run_already_posted"))
def s_dedupe(ctx: Ctx) -> None:
    """The same comment twice: already_posted, the same ids, one comment; a new key posts again."""
    body = ctx.text("once", "Dedupe check.")
    first = ctx.comment("once", body)
    ctx.want(first, 0)
    dry = ctx.runner.pc(
        "dry-again",
        "comment",
        ctx.ref,
        "--body-file",
        "-",
        "--dry-run",
        stdin=body.encode(),
        write=False,
    )
    ctx.observe(
        "gw.dry_run_already_posted", "dry_run_already_posted", _doc(dry.doc, "already_posted")
    )
    second = ctx.comment("once", body)
    ctx.want(second, 0)
    ctx.check(
        _doc(second.doc, "already_posted") is True,
        "the repeat reports already_posted",
        "fk.dedupe_hit",
    )
    ctx.check(
        sorted(ctx.ids(first)) == sorted(ctx.ids(second)),
        "the repeat reports the same ids",
        "fk.dedupe_hit",
    )
    if ctx.live:
        ctx.check(len(ctx.posted("once")) == 1, "exactly one comment on the PR", "fk.dedupe_hit")
    ctx.observe("fk.dedupe_hit", "follow_up_done", _doc(second.doc, "follow_up_done"))
    forced = ctx.comment("once", body, "--dedupe-key", f"live.{ctx.settings.run_id}.again")
    ctx.want(forced, 0)
    ctx.check(
        _doc(forced.doc, "already_posted") in (False, None),
        "a new key posts again",
        "sc.yes_dedupe",
    )
    if ctx.live:
        ctx.check(len(ctx.posted("once")) == 2, "two comments after the new key", "sc.yes_dedupe")


BODY_VECTOR = (
    "First line\n"
    "  indented, trailing spaces   \n"
    "/not-a-command\n"
    "unicode: \u00e9 \u6f22\u5b57 \U0001f600 \uff20name \uff1cb\uff1e\n"
    "forgectl:dedupe:lookalike\n"
)


@scenario(
    rows=(
        "fk.body_vectors",
        "ts.dedupe_line_format",
        "ts.stdin_trailing_newline",
        "ts.neutralizing_equal",
        "ts.host_keeps_bytes",
    )
)
def s_body_roundtrip(ctx: Ctx) -> None:
    """The stored text equals pr-comment's plan plus forgectl's dedupe line; newline handling."""
    # one tagged body for both variants (they differ only in the trailing newline), each posted
    # under its own dedupe key; the stored texts are compared with the dedupe line taken out
    label = "vec"
    base = ctx.text(label, BODY_VECTOR + "last line")
    stored_without_line: dict[str, str] = {}
    for name, suffix in (("with-newline", "\n"), ("no-newline", "")):
        body = base + suffix
        key_flag = ("--dedupe-key", f"live.{ctx.settings.run_id}.vec-{name}")
        plan = ctx.runner.pc(
            f"plan-{name}",
            "comment",
            ctx.ref,
            "--body-file",
            "-",
            "--dry-run",
            *key_flag,
            stdin=body.encode(),
            write=False,
        )
        out = ctx.comment(f"{label}-{name}", body, *key_flag)
        ctx.want(out, 0)
        text = _doc(plan.doc, "plan", "bodies")
        planned = text[0]["text"] if isinstance(text, list) and text else None
        key = _doc(out.doc, "dedupe_key")
        line = f"<!-- forgectl:dedupe:{key} -->"
        found = [c for c in ctx.posted(label) if line in str(c.get("body", ""))] if ctx.live else []
        stored = str(found[0].get("body", "")) if found else None
        if ctx.live:
            ctx.check(
                stored is not None, f"{name}: the comment is on the PR", "ts.host_keeps_bytes"
            )
        if ctx.live and stored is not None and planned is not None:
            ctx.check(
                stored.startswith(planned),
                "the stored text starts with the planned text",
                "ts.host_keeps_bytes",
            )
            rest = stored[len(planned) :]
            ctx.check(
                rest.strip() == line, "the rest is forgectl's dedupe line", "ts.dedupe_line_format"
            )
            ctx.observe("ts.dedupe_line_format", f"separator_{name}", rest.replace(line, "<line>"))
            ctx.check(
                helpers.has_marker(stored), "marker detection finds it", "ts.dedupe_line_format"
            )
        if stored is not None:
            stored_without_line[name] = stored.replace(line, "")
    if ctx.live and len(stored_without_line) == 2:
        same = stored_without_line["with-newline"] == stored_without_line["no-newline"]
        ctx.observe("ts.stdin_trailing_newline", "stored_equal_with_and_without_newline", same)
        ctx.check(
            same, "a trailing newline on stdin changes the stored text", "ts.stdin_trailing_newline"
        )


@scenario(rows=("gw.early_exit_stdin", "sc.early_exit_post_path", "sc.sigpipe"), writes=False)
def s_early_exit_stdin(ctx: Ctx) -> None:
    """A review input forgectl refuses before reading all of stdin ends with its status."""
    # 240 file-level findings of 60,000 characters each are a 14 MB draft (under the 16 MiB cap)
    # whose neutralized review body is far over forgectl's 16 MiB input limit
    body = "@" * 60_000
    findings = [
        {"id": f"big-{i}", "path": "big.txt", "line": None, "body": body} for i in range(240)
    ]
    draft = ctx.draft("huge", findings, body="Early exit probe.")
    out = ctx.runner.pc(
        "huge-review",
        "review",
        ctx.ref,
        "--draft-file",
        "-",
        "--dry-run",
        stdin=json.dumps(draft).encode("utf-8"),
        write=False,
        timeout=600,
    )
    if out.real:
        ctx.observe("gw.early_exit_stdin", "exit", out.code)
        ctx.observe("gw.early_exit_stdin", "error_code", out.error_code)
        ctx.observe("sc.early_exit_post_path", "reached_forgectl", "lower_error" in out.error)
        ctx.check(out.code == 2, f"wanted exit 2, got {out.code}", "gw.early_exit_stdin")
        ctx.check(out.error_code != "internal", "an early exit is not exit 1", "sc.sigpipe")
        ctx.check(
            out.error.get("lower_error") is not None,
            "pr-comment refused the input itself, so forgectl's early exit was not exercised "
            "(raise the size)",
            "sc.early_exit_post_path",
        )


@scenario(rows=("fk.confirmation_order",), writes=False)
def s_forgectl_confirmation_order(ctx: Ctx) -> None:
    """forgectl without --yes: the dry run prints a plan, a refusal beats the exit 13 dialog."""
    body = json.dumps(
        {"event": "comment", "body": ctx.text("confirm", "No post expected.")}
    ).encode()
    dry = ctx.runner.fg(
        "dry-no-yes", "review", "--json", ctx.ref, "--file", "-", "--dry-run", stdin=body
    )
    ctx.check(
        dry.code == 0, f"a dry run needs no --yes (got exit {dry.code})", "fk.confirmation_order"
    )
    ctx.observe("fk.confirmation_order", "dry_run_keys", sorted((dry.doc or {}).keys()))
    stale = ctx.runner.fg(
        "stale-no-yes",
        "review",
        "--json",
        ctx.ref,
        "--file",
        "-",
        f"--expected-head={ZERO_SHA}",
        "--dry-run",
        stdin=body,
    )
    ctx.observe("fk.confirmation_order", "stale_dry_run_exit", stale.code)
    if stale.real:
        ctx.check(
            stale.code in (0, 20),
            "a stale head dry run refuses (20) or reports would_refuse",
            "fk.confirmation_order",
        )
    # no --yes and stdin is not a terminal: forgectl can refuse or exit 13, it cannot post
    unconfirmed = ctx.runner.fg(
        "write-no-yes",
        "review",
        "--json",
        ctx.ref,
        "--file",
        "-",
        f"--expected-head={ZERO_SHA}",
        stdin=body,
        write=True,  # a write for the gate (a verified target) and a dry run in preflight
        yes=False,  # the point of the probe: no --yes
    )
    if unconfirmed.real:
        ctx.check(
            unconfirmed.code == 20 and unconfirmed.error.get("reason") == "stale_head",
            f"a stale head wins over confirmation (got exit {unconfirmed.code})",
            "fk.confirmation_order",
        )


# --- mentions, text safety -----------------------------------------------------------------------


def _mention_body(login: str, group: str | None) -> str:
    return "\n".join(
        [
            f"plain @{login}",
            f"letter a@{login} and digit 1@{login}",
            f"entities &amp@{login} &#64;{login} &#x40;{login} escaped \\@{login}",
            f"unterminated &#64{login} &#x40{login}",
            f"code `@{login}` and a fullwidth \uff20{login}",
            f"all @all and group @{group or 'some-group'}/sub",
            "angle \uff1cb\uff1e and bang \uff01[x](https://example.com/i.png)",
        ]
    )


@scenario(
    rows=(
        "ts.mention_filters",
        "ts.fullwidth_fold",
        "ts.entities_unterminated",
        "ts.gitlab_mentions",
    )
)
def s_mentions(ctx: Ctx) -> None:
    """Neutralized mentions stay inert on the host and the fullwidth characters are not folded."""
    login = str(ctx.whoami().get("login", "<me>"))
    body = ctx.text("mentions", _mention_body(login, ctx.settings.gitlab_group))
    out = ctx.comment("mentions", body)
    ctx.want(out, 0)
    stored = ctx.stored_body("mentions")
    if ctx.live and stored is not None:
        # recorded, not asserted: OPEN DECISION (fullwidth @) and what a host stores is its own
        ctx.observe("ts.fullwidth_fold", "fullwidth_at_kept", f"\uff20{login}" in stored)
        ctx.observe("ts.fullwidth_fold", "fullwidth_angle_kept", "\uff1cb\uff1e" in stored)
        ctx.observe("ts.fullwidth_fold", "fullwidth_bang_kept", "\uff01[x]" in stored)
        ctx.check(
            f"\n@{login}" not in stored and not stored.startswith(f"@{login}"),
            "no plain mention is stored",
            "ts.mention_filters",
        )
    control = ctx.raw_comment("control", ctx.text("control", f"positive control @{login}"))
    ctx.want(control, 0)
    ids, control_ids = ctx.ids(out), ctx.ids(control)
    html = ctx.render(ids[0])
    ref_html = ctx.render(control_ids[0])
    if html is not None and ref_html is not None and ctx.live:
        ctx.check(
            ref_html.links_to(login),
            "the positive control (a literal mention) did not link, so this check proves nothing",
            "ts.mention_filters",
        )
        ctx.check(not html.links_to(login), "a neutralized mention linked", "ts.mention_filters")
        ctx.check(not html.links_to("all"), "@all linked", "ts.gitlab_mentions")
        if ctx.settings.gitlab_group:
            ctx.check(
                not html.links_to(ctx.settings.gitlab_group),
                "a group mention linked",
                "ts.gitlab_mentions",
            )
        ctx.observe("ts.entities_unterminated", "links", len(html.links()))
        # a host that folded the fullwidth forms would show an image or a bold tag (<b>)
        ctx.observe("ts.fullwidth_fold", "img_tags", html.count("img"))
        ctx.observe("ts.fullwidth_fold", "b_tags", html.count("b"))
    elif ctx.live:
        ctx.observe("ts.mention_filters", "render", "unavailable: do the manual step")


@scenario(rows=("ts.fence_255", "ts.run_split", "ts.degraded_suggestion"))
def s_fence_limit(ctx: Ctx) -> None:
    """Degraded suggestions with backtick and tilde runs of 254, 255, 256 and 300 characters."""
    runs = [(char, size) for char in "`~" for size in (254, 255, 256, 300)]
    lines = ctx.diff_lines(len(runs) + 3)
    findings = []
    expected: dict[
        str, tuple[int, int]
    ] = {}  # opening words of a body -> (backtick run, tilde run)
    for index, (char, size) in enumerate(runs):
        kind = "backticks" if char == "`" else "tildes"
        path, line = lines[index]
        opening = f"A run of {size} {kind}."
        expected[opening] = (size, 0) if char == "`" else (0, size)
        findings.append(
            {
                "id": f"run-{'bt' if char == '`' else 'tl'}-{size}",
                "path": path,
                "line": line,
                "body": f"{opening} {ctx.label_tag('fences')}",
                "suggestion": {"lines": ["x" + char * size]},
            }
        )
    path, line = lines[len(runs)]
    both = "Backtick and tilde runs of 255 in one suggestion."
    expected[both] = (255, 255)
    findings.append(
        {
            "id": "run-both-255",
            "path": path,
            "line": line,
            "body": f"{both} {ctx.label_tag('fences')}",
            "suggestion": {"lines": ["a" + "`" * 255, "b" + "~" * 255]},
        }
    )
    # controls: a short degraded suggestion (the baseline shape) and an applicable one
    path, line = lines[len(runs) + 1]
    findings.append(
        {
            "id": "control-degraded",
            "path": path,
            "line": line,
            "body": f"Control degraded. {ctx.label_tag('fences')}",
            "suggestion": {"lines": ["x ``` y"]},
        }
    )
    path, line = lines[len(runs) + 2]
    findings.append(
        {
            "id": "control-applicable",
            "path": path,
            "line": line,
            "body": f"Control applicable. {ctx.label_tag('fences')}",
            "suggestion": {"lines": ["replacement()"]},
        }
    )
    draft = ctx.draft("fences", findings, body="Fence length probe.")
    out = ctx.post_review("fences", draft)
    ctx.want(out, 0)
    degraded = _doc(out.doc, "suggestions_degraded")
    ctx.check(
        isinstance(degraded, list)
        and sorted(str(d.get("id")) for d in degraded)
        == sorted(str(f["id"]) for f in findings if f["id"] != "control-applicable")
        and all(d.get("reason") == "fence" for d in degraded),
        "every suggestion but the applicable control is degraded for its fence",
        "ts.degraded_suggestion",
    )
    ctx.check(
        len(_doc(out.doc, "posted_inline") or []) == len(findings),
        "every finding posted inline",
        "ts.fence_255",
    )
    if not ctx.live:
        return
    comments = {
        opening: c
        for c in ctx.posted("fences")
        for opening in (*expected, "Control degraded.", "Control applicable.")
        if str(c.get("body", "")).startswith(opening)
    }
    ctx.check(len(comments) == len(findings), "the inline comments are on the PR", "ts.fence_255")
    for opening, (backticks, tildes) in expected.items():
        comment = comments.get(opening)
        if comment is None:
            continue
        problems = helpers.fence_problems(str(comment["body"]), backticks, tildes)
        ctx.check(not problems, f"{opening} stored wrongly: {'; '.join(problems)}", "ts.run_split")
    rendered: dict[str, helpers.Html] = {}
    for opening, comment in comments.items():
        html = ctx.render(str(comment["id"]))
        if html is None:
            continue
        rendered[opening] = html
        ctx.observe("ts.fence_255", f"pre_blocks:{opening}", html.count("pre"))
        ctx.check(
            "<!-- pr-comment" not in html.text, "the marker became visible text", "ts.fence_255"
        )
    control = rendered.get("Control degraded.")
    if control is None:
        ctx.observe("ts.fence_255", "render", "unavailable: do the manual step")
        return
    ctx.check(control.count("pre") == 1, "the control is not one code block", "ts.fence_255")
    for opening in expected:
        html = rendered.get(opening)
        if html is not None:
            ctx.check(
                html.count("pre") == 1 and html.shape() == control.shape(),
                f"{opening} does not render as the control's one code block",
                "ts.fence_255",
            )
    applicable = rendered.get("Control applicable.")
    if applicable is not None:
        ctx.observe(
            "ts.fence_255",
            "degraded_shape_differs_from_applicable",
            control.shape() != applicable.shape(),
        )


@scenario(rows=("ts.degraded_suggestion", "ts.applicable_suggestion", "ts.moved_finding"))
def s_review_suggestions(ctx: Ctx) -> None:
    """A review with an applicable suggestion, a degraded one and a moved finding."""
    lines = ctx.diff_lines(3)
    (p1, l1), (p2, l2), (p3, l3) = lines
    findings = [
        {
            "id": "applicable",
            "path": p1,
            "line": l1,
            "body": f"One applicable line. {ctx.label_tag('sug')}",
            "suggestion": {"lines": ["replacement()"]},
        },
        {
            "id": "degraded",
            "path": p2,
            "line": l2,
            "body": f"A fence in the suggestion. {ctx.label_tag('sug')}",
            "suggestion": {"lines": ["x ``` y"]},
        },
        {"id": "gone", "path": p3, "line": 2_000_000, "body": "A line that is not in the diff."},
    ]
    out = ctx.post_review("sug", ctx.draft("sug", findings, body="Suggestion kinds."))
    ctx.want(out, 0)
    ctx.check(
        sorted(_doc(out.doc, "posted_inline") or []) == ["applicable", "degraded"],
        "two findings inline",
        "ts.applicable_suggestion",
    )
    moved = _doc(out.doc, "moved") or []
    ctx.check(
        [m.get("id") for m in moved] == ["gone"],
        "the missing line moved to the body",
        "ts.moved_finding",
    )
    ctx.check(
        [d.get("id") for d in _doc(out.doc, "suggestions_degraded") or []] == ["degraded"],
        "one degraded",
        "ts.degraded_suggestion",
    )
    if ctx.live:
        stored = [str(c.get("body", "")) for c in ctx.posted("sug")]
        want = "```suggestion\n" if ctx.github else "```suggestion:-0+0\n"
        ctx.check(
            any(want in b for b in stored),
            f"an inline body holds {want.strip()}",
            "ts.applicable_suggestion",
        )
        ctx.check(
            any(b.count("```text") >= 1 for b in stored),
            "the degraded block is a text block",
            "ts.degraded_suggestion",
        )


@scenario(rows=("ts.suggestion_infostring", "ts.suggestion_longer_fence", "ts.suggestion_variants"))
def s_suggestion_probes(ctx: Ctx) -> None:
    """Raw review comments: which fence forms a host treats as an applicable suggestion."""
    (path, line), *_ = ctx.diff_lines(1)
    info = "suggestion" if ctx.github else "suggestion:-0+0"
    forms = {
        "control-suggestion": f"```{info}\nreplacement()\n```",
        "control-code": "```text\nreplacement()\n```",
        "longer-fence": f"````{info}\nreplacement()\n````",
        "tilde-fence": f"~~~{info}\nreplacement()\n~~~",
        "info-after-word": "```python suggestion\nreplacement()\n```",
        "word-then-suggestion": f"```text {info}\nreplacement()\n```",
        "indented-fence": f"  ```{info}\nreplacement()\n  ```",
    }
    comments = [
        {"path": path, "line": line, "body": f"{name}\n{text}\n{ctx.label_tag(name)}"}
        for name, text in forms.items()
    ]
    out = ctx.raw_review(
        "probes", {"event": "comment", "body": ctx.label_tag("probes"), "comments": comments}
    )
    ctx.want(out, 0)
    if not ctx.live:
        return
    by_name = {
        str(c["body"]).split("\n", 1)[0]: c
        for c in ctx.comments()
        if str(c.get("body", "")).split("\n", 1)[0] in forms
    }
    shapes = {}
    for name, comment in by_name.items():
        html = ctx.render(str(comment["id"]))
        if html is not None:
            shapes[name] = html.shape()
    if "control-suggestion" in shapes and "control-code" in shapes:
        # recorded, not asserted: a host may render both alike (then no variant can be classified)
        distinct = shapes["control-suggestion"] != shapes["control-code"]
        ctx.observe("ts.suggestion_variants", "controls_distinguishable", distinct)
        for name, shape in shapes.items():
            verdict = (
                "indistinguishable"
                if not distinct
                else "applicable"
                if shape == shapes["control-suggestion"]
                else "code"
                if shape == shapes["control-code"]
                else "other"
            )
            row = {
                "longer-fence": "ts.suggestion_longer_fence",
                "info-after-word": "ts.suggestion_infostring",
                "word-then-suggestion": "ts.suggestion_infostring",
            }.get(name, "ts.suggestion_variants")
            ctx.observe(
                row, name, verdict
            )  # OPEN DECISION: the fence degrade; the suite only records
    else:
        ctx.observe("ts.suggestion_variants", "render", "unavailable: do the manual step")


@scenario(
    rows=(
        "ts.github_media",
        "ts.github_diagrams",
        "ts.link_forms",
        "ts.postprocess",
        "ts.marker_display",
    )
)
def s_render_probes(ctx: Ctx) -> None:
    """How the host renders a media URL, diagram fences, odd links, emoji and the marker."""
    login = str(ctx.whoami().get("login", "<me>"))
    body = ctx.text(
        "render",
        "\n".join(
            [
                "https://github.com/user-attachments/assets/00000000-0000-0000-0000-000000000000",
                "https://example.com/files/clip.mp4",
                "```mermaid\ngraph TD; A-->B\n```",
                "```geojson\n{}\n```",
                "```stl\nsolid x\nendsolid x\n```",
                "[js](javascript:alert(1)) [rel](relative/path) [auto](https://example.com)",
                ":smile: #1 0123456789abcdef0123456789abcdef01234567",
                f"[@{login}](https://example.com) 1@{login} a@{login}",
                "![img](https://example.com/i.png)",
            ]
        ),
    )
    out = ctx.comment("render", body)
    ctx.want(out, 0)
    html = ctx.render(ctx.ids(out)[0])
    if not (ctx.live and html is not None):
        ctx.observe("ts.postprocess", "render", "unavailable: do the manual step")
        return
    ctx.observe("ts.link_forms", "javascript_hrefs", html.hrefs_with_scheme("javascript:"))
    ctx.check("pr-comment:v1" not in html.text, "the marker is visible text", "ts.marker_display")
    ctx.observe("ts.github_media", "img_tags", html.count("img"))
    ctx.observe("ts.github_media", "video_tags", html.count("video"))
    ctx.observe("ts.github_diagrams", "pre_tags", html.count("pre"))
    ctx.observe("ts.link_forms", "links", [a.get("href", "")[:40] for a in html.links()])
    ctx.observe("ts.postprocess", "mention_links", len(html.links_to(login)))
    ctx.observe("ts.marker_display", "marker_comment_in_text", "pr-comment:v1" in html.text)


@scenario(rows=("ts.gitlab_engine",), backends=GITLAB)
def s_gitlab_markdown(ctx: Ctx) -> None:
    """GitLab's own Markdown: ``>>>`` quotes, [[_TOC_]], math, a slash line (a quick action)."""
    body = ctx.text(
        "gitlab-md",
        "\n".join(
            [
                ">>>\nquoted\n>>>",
                "[[_TOC_]]",
                "$`a^2`$",
                "```math\na^2\n```",
                "/shrug",
                "references: #1 !1 ~label %milestone",
            ]
        ),
    )
    out = ctx.comment("gitlab-md", body)
    ctx.want(out, 0)
    stored = ctx.stored_body("gitlab-md")
    if ctx.live and stored is not None:
        ctx.check(
            not stored.startswith("/shrug") and "\n/shrug" not in stored,
            "the slash line was not neutralized",
            "ts.gitlab_engine",
        )
        ctx.check(
            "\u00af\\_(\u30c4)_/\u00af" not in stored,
            "a quick action ran (/shrug expanded)",
            "ts.gitlab_engine",
        )
    html = ctx.render(ctx.ids(out)[0])
    if html is not None and ctx.live:
        ctx.observe("ts.gitlab_engine", "blockquote_tags", html.count("blockquote"))
        ctx.observe("ts.gitlab_engine", "links", len(html.links()))


@scenario(rows=("ts.line_separators",))
def s_line_separators(ctx: Ctx) -> None:
    """U+2028, U+2029, VT, FF and NEL in a body: stored as sent, and not line breaks on the host."""
    body = ctx.text("seps", "one\u2028two\u2029three")
    out = ctx.comment("seps", body)
    ctx.observe("ts.line_separators", "pr_comment_exit", out.code)
    raw_text = "raw\u2028two\u2029three\x0bvt\x0cff\u0085nel\n" + ctx.label_tag("raw-seps")
    raw = ctx.raw_comment("raw-seps", raw_text)
    ctx.observe("ts.line_separators", "forgectl_exit", raw.code)
    if ctx.live and raw.real:
        stored = ctx.stored_body("raw-seps")
        ctx.observe(
            "ts.line_separators", "stored_repr", None if stored is None else ascii(stored[:60])
        )
        html = ctx.render(ctx.ids(raw)[0]) if raw.code == 0 else None
        if html is not None:
            ctx.observe("ts.line_separators", "br_tags", html.count("br"))
            ctx.observe("ts.line_separators", "p_tags", html.count("p"))


# --- reviews, threads, replies -------------------------------------------------------------------


@scenario(rows=("fk.gitlab_no_body_marker", "fk.id_prefixes"), backends=GITLAB)
def s_gitlab_review_no_body(ctx: Ctx) -> None:
    """A GitLab review whose body is only the label: review_id is null."""
    ((path, line),) = ctx.diff_lines(1)
    finding = {
        "id": "f1",
        "path": path,
        "line": line,
        "body": f"Inline only. {ctx.label_tag('nobody')}",
    }
    draft = ctx.draft("nobody", [finding], tag_body=False)
    out = ctx.post_review("nobody", draft)
    ctx.want(out, 0)
    ctx.check(_doc(out.doc, "review_id") is None, "GitLab has no review id", "fk.id_prefixes")
    if ctx.live:
        inline = ctx.posted("nobody")
        ctx.check(
            bool(inline) and helpers.has_marker(str(inline[-1].get("body", ""))),
            "the marker is on the inline comment",
            "fk.gitlab_no_body_marker",
        )


@scenario(
    rows=("th.gitlab_body_thread", "th.gl_d_ids", "th.changed_false", "th.not_found"),
    backends=GITLAB,
)
def s_gitlab_body_thread(ctx: Ctx) -> None:
    """The GitLab body thread: resolved by default, then resolve (no-op), unresolve, resolve."""
    draft = ctx.draft("bodythread", [], body="A summary that opens a thread.")
    out = ctx.post_review("bodythread", draft)
    ctx.want(out, 0)
    thread = ctx.value(out, "body_thread_id", "thread_id")
    ctx.check(
        isinstance(thread, str) and thread.startswith("gl:d:"),
        "a gl:d: body thread id",
        "th.gl_d_ids",
    )
    ctx.check(
        _doc(out.doc, "body_thread_resolved") is True,
        "resolved by default",
        "th.gitlab_body_thread",
    )
    again = ctx.runner.pc("resolve-again", "resolve", ctx.ref, "--thread", str(thread))
    ctx.want(again, 0)
    if ctx.live and again.doc:
        ctx.check(
            again.doc["threads"][0].get("changed") is False,
            "already resolved: changed false",
            "th.changed_false",
        )
    un = ctx.runner.pc("unresolve", "unresolve", ctx.ref, "--thread", str(thread))
    ctx.want(un, 0)
    if ctx.live and un.doc:
        ctx.check(
            un.doc["threads"][0].get("changed") is True, "unresolve changes it", "th.gl_d_ids"
        )
    res = ctx.runner.pc("resolve", "resolve", ctx.ref, "--thread", str(thread))
    ctx.runner.leave(f"the GitLab body thread of {ctx.ref} (a note and a thread resolved)")
    ctx.want(res, 0)
    missing = ctx.runner.pc("missing", "resolve", ctx.ref, "--thread", "gl:d:" + "0" * 40)
    ctx.want(missing, 10, "not_found", row="th.not_found")


@scenario(
    rows=("th.not_found", "th.changed_false", "th.thread_author_is_self", "fk.id_prefixes"),
    backends=GITHUB,
)
def s_resolve_unresolve(ctx: Ctx) -> None:
    """A GitHub review thread: resolve, resolve again, unresolve, and a thread not on the PR."""
    ((path, line),) = ctx.diff_lines(1)
    out = ctx.post_review(
        "thread",
        ctx.draft(
            "thread",
            [
                {
                    "id": "t1",
                    "path": path,
                    "line": line,
                    "body": f"A thread to resolve. {ctx.label_tag('thread')}",
                }
            ],
            body="Thread probe.",
        ),
    )
    ctx.want(out, 0)
    ctx.observe("fk.id_prefixes", "thread_ids_on_review", _doc(out.doc, "thread_ids"))
    thread = "<thread_id>"
    if ctx.live:
        found = [
            t
            for t in ctx.threads()
            if any(
                ctx.label_tag("thread") in str(c.get("body", "")) for c in t.get("comments") or []
            )
        ]
        ctx.check(bool(found), "the inline comment is in a listed thread", "th.changed_false")
        thread = str(found[0]["id"]) if found else thread
        ctx.check(thread.startswith("gh:rt:"), "a gh:rt: thread id", "fk.id_prefixes")
    first = ctx.runner.pc("resolve", "resolve", ctx.ref, "--thread", thread)
    ctx.runner.leave(f"a review thread of {ctx.ref} that was resolved and unresolved again")
    ctx.want(first, 0)
    second = ctx.runner.pc("resolve-again", "resolve", ctx.ref, "--thread", thread)
    ctx.want(second, 0)
    if ctx.live and second.doc:
        ctx.check(
            second.doc["threads"][0].get("changed") is False,
            "no-op reports changed false",
            "th.changed_false",
        )
        ctx.observe(
            "th.thread_author_is_self",
            "resolved_own_thread",
            first.doc["threads"][0] if first.doc else None,
        )
    un = ctx.runner.pc("unresolve", "unresolve", ctx.ref, "--thread", thread)
    ctx.want(un, 0)
    missing = ctx.runner.pc("missing", "resolve", ctx.ref, "--thread", "gh:rt:PRRT_kwDOZZZZZZZZZZ")
    ctx.want(missing, 10, "not_found", row="th.not_found")


@scenario(rows=("fk.id_prefixes", "th.reply_threaded"))
def s_reply_kinds(ctx: Ctx) -> None:
    """Replies to a general comment, an inline thread and a review or note: the threaded rule."""
    general = ctx.comment("target", ctx.text("target", "A comment to reply to."))
    ctx.want(general, 0)
    ((path, line),) = ctx.diff_lines(1)
    review = ctx.post_review(
        "replies",
        ctx.draft(
            "replies",
            [
                {
                    "id": "r1",
                    "path": path,
                    "line": line,
                    "body": f"Inline to reply to. {ctx.label_tag('replies')}",
                }
            ],
            body="Reply targets.",
        ),
    )
    ctx.want(review, 0)
    targets: dict[str, str] = {"general": ctx.ids(general)[0]}
    if ctx.live:
        inline = [c for c in ctx.posted("replies") if "Inline to reply to." in str(c["body"])]
        if inline:
            targets["inline_comment"] = str(inline[0]["id"])
            targets["inline_thread"] = str(inline[0]["_thread"]["id"])
        review_id = _doc(review.doc, "review_id")
        if isinstance(review_id, str):
            targets["review"] = review_id
    else:
        targets["inline_thread"] = "<thread_id>"
    for kind, to in targets.items():
        out = ctx.reply(kind, to, ctx.text(f"reply-{kind}", f"Reply to the {kind}."))
        ctx.want(out, 0)
        threaded = _doc(out.doc, "threaded")
        ctx.observe("th.reply_threaded", kind, threaded)
        if ctx.github and ctx.live:
            expected = kind in ("inline_comment", "inline_thread")
            ctx.check(
                threaded is expected,
                f"GitHub reply to {kind}: threaded must be {expected}",
                "th.reply_threaded",
            )
        elif ctx.live:
            ctx.check(isinstance(threaded, bool), "threaded is a boolean", "th.reply_threaded")


# --- stale heads, re-anchoring, refusals ---------------------------------------------------------


@scenario(rows=("st.stale_head", "st.reanchor"), needs=("old_head",))
def s_stale_and_reanchor(ctx: Ctx) -> None:
    """A draft written at an older head: stale_head, then a re-anchored post."""
    old = ctx.target.old_head if ctx.target else None
    ((path, line),) = ctx.diff_lines(1)
    findings = [{"id": "old", "path": path, "line": line, "body": "Written at an older head."}]
    draft = ctx.draft("stale", findings, body="Older head.", head=old)
    refused = ctx.post_review("stale", draft)
    ctx.want(refused, 20, "stale_head", row="st.stale_head")
    current = str(_doc(ctx.show(), "head", "sha"))
    posted = ctx.post_review("stale", draft, "--reanchor-to", current)
    ctx.want(posted, 0, row="st.reanchor")
    if ctx.live:
        ids = {f["id"] for f in findings}
        landed = set(_doc(posted.doc, "posted_inline") or []) | {
            m["id"] for m in _doc(posted.doc, "moved") or []
        }
        ctx.check(ids == landed, "every finding posted inline or moved", "st.reanchor")
        ctx.check(
            _doc(posted.doc, "reanchored_draft") is not None,
            "the re-anchored draft is returned",
            "st.reanchor",
        )
        ctx.check(
            _doc(posted.doc, "head_sha") == current,
            "the review is pinned to the current head",
            "st.reanchor",
        )
    again = ctx.post_review("stale", draft, "--reanchor-to", current)
    ctx.want(again, 0, row="st.reanchor")
    ctx.check(_doc(again.doc, "already_posted") is True, "the rerun posts nothing", "st.reanchor")


@scenario(rows=("fk.codes_reasons", "th.not_found", "fk.plan_event_null_approve"), writes=False)
def s_refusals(ctx: Ctx) -> None:
    """Refusals that write nothing: stale head and base, an unknown reply target, a missing PR."""
    body = ctx.text("refuse", "Refusal probe: never posted.").encode()
    human = "human:live-suite"  # an auto: approve is refused by the guard before the checks below
    if ctx.may_approve:
        # an approve is a write if the checks were ever to pass: only with PR_COMMENT_LIVE_APPROVE=1
        stale = ctx.runner.pc(
            "stale-head",
            "approve",
            ctx.ref,
            f"--expected-head={ZERO_SHA}",
            "--expected-base=main",
            write=True,
            actor=human,
        )
        ctx.want(stale, 20, "stale_head", row="fk.codes_reasons")
        base = ctx.runner.pc(
            "stale-base",
            "approve",
            ctx.ref,
            f"--expected-head={_doc(ctx.show(), 'head', 'sha')}",
            "--expected-base=no-such-branch-live",
            write=True,
            actor=human,
        )
        ctx.want(base, 20, "stale_base", row="fk.codes_reasons")
    else:
        ctx.observe(
            "fk.codes_reasons",
            "approve_refusals",
            "skipped: they are approve writes (set PR_COMMENT_LIVE_APPROVE=1)",
        )
    to = ctx.runner.pc(
        "unknown-target",
        "reply",
        ctx.ref,
        "--to",
        "gh:rc:RC_kwDOZZZZZZZZ" if ctx.github else "gl:n:2147483000",
        "--body-file",
        "-",
        stdin=body,
        write=True,
    )
    ctx.want(to, 10, "not_found", row="th.not_found")
    # no such PR, so there is nothing for show to confirm: the repo as written is checked
    absent = f"{ctx.target.repo if ctx.target else 'host/owner/repo'}#99999999"
    ctx.runner.allow_ref_text(absent)
    missing = ctx.runner.pc(
        "no-such-pr", "comment", absent, "--body-file", "-", stdin=body, write=True
    )
    ctx.want(missing, 10, "not_found", row="fk.codes_reasons")
    own = ctx.runner.pc(
        "approve-dry",
        "approve",
        ctx.ref,
        f"--expected-head={_doc(ctx.show(), 'head', 'sha')}",
        "--expected-base=" + str(_doc(ctx.show(), "base", "ref")),
        "--dry-run",
        write=False,
        actor=human,
    )
    if own.real and own.doc and own.code == 0:
        ctx.check(
            _doc(own.doc, "plan", "event") is None,
            "plan.event is null for approve",
            "fk.plan_event_null_approve",
        )
    ctx.observe(
        "fk.codes_reasons", "approve_dry_run", {"exit": own.code, "reason": own.error.get("reason")}
    )


@scenario(rows=("fk.codes_reasons",), needs=("closed_ref",))
def s_closed_pr(ctx: Ctx) -> None:
    """A closed or merged PR: review and approve are refused not_open, a comment still posts."""
    closed = (ctx.target.closed_ref if ctx.target else None) or ctx.ref
    ctx.runner.verify_target(closed)
    sub = ctx.retarget(closed)
    state = _doc(sub.show(), "state")
    if ctx.live and state == "open":
        # a review of an open PR would post: nothing is written unless it is really not open
        raise LiveRefusal(
            "the closed-PR target is open (or could not be read); nothing was written"
        )
    draft = sub.draft("closed", [], body="Closed PR probe.")
    review = sub.post_review("closed", draft)
    sub.want(review, 20, "not_open", row="fk.codes_reasons")
    if ctx.may_approve:
        head = str(_doc(sub.show(), "head", "sha"))
        approve = ctx.runner.pc(
            "approve-closed",
            "approve",
            sub.ref,
            f"--expected-head={head}",
            f"--expected-base={_doc(sub.show(), 'base', 'ref')}",
            write=True,
            actor="human:live-suite",
        )
        sub.want(approve, 20, "not_open", row="fk.codes_reasons")
    else:
        ctx.observe(
            "fk.codes_reasons",
            "approve_not_open",
            "skipped: an approve write (set PR_COMMENT_LIVE_APPROVE=1)",
        )
    comment = sub.comment("closed", sub.text("closed", "A comment on a closed PR."))
    sub.want(comment, 0, row="fk.codes_reasons")


# --- approvals -----------------------------------------------------------------------------------


@scenario(rows=("fk.approve_interactive",), needs=("approve",))
def s_approve_interactive(ctx: Ctx) -> None:
    """A human approval on a pty: declined once, then confirmed. Not undone by the suite."""
    ref = ctx.target.approve_ref if ctx.target else ctx.ref
    ctx.runner.verify_target(ref or ctx.ref)
    body = ctx.runner.workdir / "approve-body.txt"
    body.write_text(ctx.text("approve", "Approval from the live suite."), encoding="utf-8")
    declined = ctx.runner.pc_pty(
        "approve-declined", "approve", ref or ctx.ref, "--body-file", str(body), answer=b"n\n"
    )
    ctx.want(declined, 13, "confirmation_required", row="fk.approve_interactive")
    approved = ctx.runner.pc_pty(
        "approve-confirmed", "approve", ref or ctx.ref, "--body-file", str(body), answer=b"y\n"
    )
    ctx.runner.leave(f"an approval on {ref or ctx.ref}: dismiss or unapprove it by hand")
    ctx.want(approved, 0, row="fk.approve_interactive")
    ctx.check(
        _doc(approved.doc, "event") == "approve",
        "the result says approve",
        "fk.approve_interactive",
    )


# --- signals, hooks, escalation, config ----------------------------------------------------------


@scenario(rows=("gw.signal_term", "fk.signal_after_write"))
def s_signal_mid_write(ctx: Ctx) -> None:
    """SIGTERM, SIGHUP and SIGINT after the attempted record: outcome_unknown, one post."""
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        label = f"sig-{sig.name.lower()}"
        body = ctx.text(label, f"Signal probe {sig.name}.").encode()
        args = ("comment", ctx.ref, "--body-file", "-")
        # the audit records of earlier launches are not this one's: count before starting it
        before = ctx.runner.audit_count("attempted")
        running = ctx.runner.start_pc(label, *args, stdin=body)
        if running.proc is not None:
            ctx.check(
                ctx.runner.wait_for_audit("attempted", after=before),
                "no attempted record appeared for this run",
                "fk.signal_after_write",
            )
            running.send(sig)
        result = running.finish()
        if result.real:
            ctx.observe("gw.signal_term", f"{sig.name}_exit", result.code)
            ctx.observe("gw.signal_term", f"{sig.name}_code", result.error_code)
            ctx.check(
                result.code in (0, 12, 128 + int(sig)),
                f"{sig.name}: unexpected exit {result.code}",
                "fk.signal_after_write",
            )
            if result.code == 12:
                ctx.check(
                    result.error_code == "outcome_unknown",
                    f"{sig.name}: exit 12 is outcome_unknown",
                    "fk.signal_after_write",
                )
                ctx.check(
                    result.error.get("retry") == "same_command",
                    "retry is same_command",
                    "fk.signal_after_write",
                )
        rerun = ctx.runner.pc(f"{label}-rerun", *args, stdin=body)
        ctx.want(rerun, 0, row="fk.signal_after_write")
        if ctx.live:
            ctx.check(
                len(ctx.posted(label)) == 1,
                f"{sig.name}: exactly one comment after the rerun",
                "fk.signal_after_write",
            )


ASYNC_SLEEP_SECONDS = 20  # long enough that returning before it ends cannot be a race


def async_hook_config(marker: object) -> str:
    """pr-comment config with one async ``post-post`` hook that touches ``marker`` late."""
    command = f"sleep {ASYNC_SLEEP_SECONDS}; touch {marker}"
    return f"hooks:\n  post-post:\n    - cmd: {json.dumps(command)}\n      async: true\n"


def _kill_leaked_sleep(pid: int) -> None:
    """End the hook's ``sleep 300`` if it is still there: only that pid (read from the hook's own
    pid file) and only while its command line is the one the hook started."""
    try:
        listing = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if listing.stdout.strip() == "sleep 300":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


AMBIENT_TOKENS = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITLAB_TOKEN",
    "GITLAB_ACCESS_TOKEN",
    "OAUTH_TOKEN",
)


@scenario(rows=("hk.decision_ids", "hk.scrubbed_env"))
def s_hooks_pass_through(ctx: Ctx) -> None:
    """A pre-post hook's decision id reaches forgectl; its environment holds no secret."""
    dump = ctx.runner.workdir / "hook-env.txt"
    decision = f"live-decision-{ctx.settings.run_id}"
    stdout = json.dumps({"decision_id": decision})
    hook = f"cat >/dev/null; env | cut -d= -f1 | sort > {dump}; echo '{stdout}'"
    ctx.tap_since()
    # a canary in an ambient-token variable the target's host does not use: forgectl's host
    # tools read GH_*, GITHUB_* for GitHub and GITLAB_*, OAUTH_TOKEN for GitLab, so the other
    # family cannot change how this run authenticates
    canary = "GITLAB_ACCESS_TOKEN" if ctx.github else "GITHUB_ENTERPRISE_TOKEN"
    injected = canary not in ctx.runner.env
    out = ctx.runner.pc(
        "hook-comment",
        "comment",
        ctx.ref,
        "--body-file",
        "-",
        "--hook",
        f"pre-post={hook}",
        stdin=ctx.text("hook", "A hook ran before this.").encode(),
        extra_config="hook_env_deny:\n  - PRC_LIVE_DENY_ME\n",
        env_extra={
            "PRC_LIVE_DENY_ME": "x",
            "PRC_LIVE_KEEP_ME": "y",
            **({canary: f"canary-{ctx.settings.run_id}"} if injected else {}),
        },
    )
    ctx.want(out, 0)
    if ctx.live:
        sent = [a for argv in ctx.tap_since() for a in argv]
        ctx.check(
            f"--decision-id={decision}" in sent,
            "the hook's id reached forgectl as --decision-id=",
            "hk.decision_ids",
        )
        ctx.check(dump.exists(), "the hook did not run", "hk.decision_ids")
        names = set(dump.read_text(encoding="utf-8").split()) if dump.exists() else set()
        ambient = {n for n in AMBIENT_TOKENS if n in ctx.runner.env} | (
            {canary} if injected else set()
        )
        ctx.check(
            not (ambient & names),
            f"ambient token variables reached a hook: {sorted(ambient & names)}",
            "hk.scrubbed_env",
        )
        ctx.check("PRC_LIVE_DENY_ME" not in names, "hook_env_deny did not apply", "hk.scrubbed_env")
        ctx.check(
            "PRC_LIVE_KEEP_ME" in names,
            "an ordinary variable did not reach the hook",
            "hk.scrubbed_env",
        )
        ctx.check("PR_COMMENT_HOOK" in names, "PR_COMMENT_HOOK is missing", "hk.scrubbed_env")
        ctx.observe("hk.scrubbed_env", "ambient_tokens_set_in_this_run", sorted(ambient))
        ctx.observe("hk.scrubbed_env", "canary_injected", injected)


@scenario(rows=("hk.group_kill", "hk.async"), writes=False)
def s_hooks_timeout_and_async(ctx: Ctx) -> None:
    """A hook that times out is vetoed and its children die; an async hook is not waited for."""
    work = ctx.runner.workdir
    pidfile = work / "hook-child.pid"
    config = (
        "hooks:\n"
        "  pre-post:\n"
        f"    - cmd: {json.dumps(f'sleep 300 & echo $! > {pidfile}; wait')}\n"
        "      timeout: 2s\n"
    )
    # the hook times out before any forgectl write, so this real call posts nothing
    veto = ctx.runner.pc(
        "hook-timeout-real",
        "comment",
        ctx.ref,
        "--body-file",
        "-",
        stdin=ctx.text("hook-veto", "never posted").encode(),
        extra_config=config,
    )
    ctx.want(veto, 11, row="hk.group_kill")
    if ctx.live:
        pid = helpers.read_pidfile(pidfile)
        ctx.check(pid > 0, "the hook did not start (no pid was written)", "hk.group_kill")
        if pid > 0:
            ctx.runner.cleanups.append(lambda: _kill_leaked_sleep(pid))
        time.sleep(1.0)
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
        ctx.check(
            not alive,
            f"the hook's background process {pid} outlived the hook (not killed by the suite)",
            "hk.group_kill",
        )
        ctx.check(len(ctx.posted("hook-veto")) == 0, "a vetoed run posted", "hk.group_kill")
    marker = work / "async-done"
    async_config = async_hook_config(marker)
    started = time.monotonic()
    posted = ctx.runner.pc(
        "hook-async",
        "comment",
        ctx.ref,
        "--body-file",
        "-",
        stdin=ctx.text("hook-async", "async hook").encode(),
        extra_config=async_config,
    )
    ctx.want(posted, 0, row="hk.async")
    if ctx.live:
        ctx.check(
            time.monotonic() - started < 120 and not marker.exists(),
            "the async hook was waited for",
            "hk.async",
        )
        ctx.observe("hk.async", "marker_after_return", marker.exists())


@scenario(rows=("eo.forgectl_scope",))
def s_override_escalation(ctx: Ctx) -> None:
    """A forgectl hook that escalates (exit 13): refused, then allowed with the override."""
    config = ctx.runner.workdir / "forgectl-escalate.yaml"
    config.write_text(
        _forgectl_hosts(ctx) + "hooks:\n  pre-post:\n    - cmd: 'exit 13'\n", encoding="utf-8"
    )
    config.chmod(0o600)
    args = ("comment", ctx.ref, "--body-file", "-")
    body = ctx.text("escalate", "Escalation probe.").encode()
    refused = ctx.runner.pc("escalated", *args, stdin=body, forgectl_config=str(config))
    ctx.want(refused, 13, "escalated", row="eo.forgectl_scope")
    ctx.check(
        refused.error.get("escalated_by") == "forgectl",
        "escalated_by is forgectl",
        "eo.forgectl_scope",
    )
    allowed = ctx.runner.pc(
        "override",
        *args,
        "--override-escalation=forgectl",
        stdin=body,
        forgectl_config=str(config),
        actor="human:live-suite",
    )
    ctx.want(allowed, 0, row="eo.forgectl_scope")
    if ctx.live:
        ctx.check(
            len(ctx.posted("escalate")) == 1, "one comment after the override", "eo.forgectl_scope"
        )


@scenario(rows=("fk.config_flag",), writes=False)
def s_config_flag(ctx: Ctx) -> None:
    """forgectl's --config: a directory or an unparsable file is config_invalid."""
    work = ctx.runner.workdir
    bad = work / "forgectl-bad.yaml"
    bad.write_text("{{{ not yaml", encoding="utf-8")
    bad.chmod(0o600)
    good = work / "forgectl-good.yaml"
    good.write_text(_forgectl_hosts(ctx) + "hooks:\n  pre-post: []\n", encoding="utf-8")
    good.chmod(0o600)
    for name, path, wanted in (
        ("directory", str(work), 2),
        ("unparsable", str(bad), 2),
        ("valid", str(good), 0),
    ):
        out = ctx.runner.pc(
            f"config-{name}",
            "comment",
            ctx.ref,
            "--body-file",
            "-",
            "--dry-run",
            stdin=b"x",
            write=False,
            forgectl_config=path,
        )
        ctx.observe("fk.config_flag", name, {"exit": out.code, "code": out.error_code})
        if out.real:
            ctx.check(
                out.code == wanted,
                f"{name}: wanted exit {wanted}, got {out.code}",
                "fk.config_flag",
            )


@scenario(rows=("gw.deadline_kill", "gw.hook_outlasts_slack"), needs=("slow",))
def s_deadline_kill(ctx: Ctx) -> None:
    """A forgectl hook that outlasts the deadline: outcome_unknown."""
    config = ctx.runner.workdir / "forgectl-slow.yaml"
    config.write_text(
        _forgectl_hosts(ctx) + "hooks:\n  pre-post:\n    - cmd: 'sleep 200'\n      timeout: 200s\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    out = ctx.runner.pc(
        "slow-hook",
        "comment",
        ctx.ref,
        "--body-file",
        "-",
        stdin=ctx.text("slow", "Deadline probe.").encode(),
        forgectl_config=str(config),
        extra_config="timeout: 5s\n",
        timeout=400,
    )
    ctx.observe("gw.deadline_kill", "exit", out.code)
    ctx.observe("gw.deadline_kill", "code", out.error_code)
    ctx.observe("gw.hook_outlasts_slack", "landed", out.error.get("landed"))
    if out.real:
        ctx.check(out.code == 12, f"wanted exit 12, got {out.code}", "gw.deadline_kill")
        ctx.check(
            len(ctx.posted("slow")) == 0,
            "the write landed although its hook never finished",
            "gw.deadline_kill",
        )


@scenario(rows=("gw.outcome_partial_landed", "fk.dedupe_hit"))
def s_dedupe_event_change(ctx: Ctx) -> None:
    """The same draft id with another event: GitHub outcome_partial, GitLab completes it."""
    ((path, line),) = ctx.diff_lines(1)
    findings = [{"id": "e1", "path": path, "line": line, "body": "Event change probe."}]
    draft = ctx.draft("evchange", findings, body="Event change.")
    first = ctx.post_review("evchange", draft)
    ctx.want(first, 0)
    second = ctx.post_review("evchange-request", {**draft, "event": "request_changes"})
    ctx.observe("gw.outcome_partial_landed", "exit", second.code)
    ctx.observe("gw.outcome_partial_landed", "code", second.error_code)
    ctx.observe("gw.outcome_partial_landed", "landed", second.error.get("landed"))
    ctx.observe("gw.outcome_partial_landed", "retry", second.error.get("retry"))
    ctx.observe("fk.dedupe_hit", "follow_up_done", _doc(second.doc, "follow_up_done"))
    if ctx.github:
        ctx.want(second, 12, "outcome_partial", row="gw.outcome_partial_landed")
        ctx.check(
            second.error.get("retry") == "new_key",
            "GitHub: retry is new_key",
            "gw.outcome_partial_landed",
        )
    else:
        ctx.check(
            second.code in (0, 12),
            "GitLab: the state step completes or is partial",
            "gw.outcome_partial_landed",
        )


@scenario(rows=("ts.unicode_whitespace_marker",))
def s_marker_whitespace(ctx: Ctx) -> None:
    """Where forgectl puts its dedupe line when the last line is a no-break space (raw probe)."""
    label = "nbsp"
    key = f"live.{ctx.settings.run_id}.nbsp"
    body = f"{ctx.label_tag(label)}\n{helpers.MARKER}\n\u00a0"
    out = ctx.runner.fg(
        "raw-nbsp",
        "comment",
        "--json",
        ctx.ref,
        "--body-file",
        "-",
        f"--dedupe-key={key}",
        "--actor",
        "auto:live-suite",
        stdin=body.encode("utf-8"),
        write=True,
    )
    ctx.want(out, 0, row="ts.unicode_whitespace_marker")
    stored = ctx.stored_body(label)
    if ctx.live and stored is not None:
        line = f"<!-- forgectl:dedupe:{key} -->"
        ctx.observe(
            "ts.unicode_whitespace_marker", "stored", ascii(stored.replace(line, "<dedupe>"))
        )
        ctx.observe("ts.unicode_whitespace_marker", "marker_detected", helpers.has_marker(stored))


@scenario(rows=("fk.diff_markers",), writes=False)
def s_diff_markers(ctx: Ctx) -> None:
    """What forgectl's diff keeps of a carriage return and of the no-newline marker (recorded)."""
    out = ctx.runner.fg("diff", "diff", "--json", ctx.ref)
    if not out.real:
        return
    diff = str(_doc(out.doc, "diff") or "")
    ctx.observe("fk.diff_markers", "exit", out.code)
    ctx.observe("fk.diff_markers", "carriage_returns", diff.count("\r"))
    ctx.observe("fk.diff_markers", "no_newline_markers", diff.count("\\ No newline"))
    ctx.observe("fk.diff_markers", "added_lines", len(helpers.added_lines(diff)))


__all__ = ["SCENARIOS", "Scenario", "pairs", "skip_reason"]
