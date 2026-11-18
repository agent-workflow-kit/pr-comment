"""The confirmation dialog's text and answer parsing (docs/interface.md, Confirmation).

The terminal behaviour (a real pty, signals, type-ahead) is in ``test_confirm_pty.py``.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Callable
from typing import Any

import pytest

from pr_comment import confirm
from pr_comment.display import sanitize_text
from pr_comment.plan import Plan, PlanBody, PlanInline, build_review_plan, build_text_plan
from pr_comment.precheck import Prechecked
from pr_comment.prepare import Prepared, Request
from tests.posting import A, B, context, draft_doc, finding, show_of, source_of

Golden = Callable[[str, str], None]


def prepared_of(
    plan: Plan, *, already_posted: bool | None = None, allow: tuple[str, ...] = ()
) -> Prepared:
    request = Request(command=plan.action, pr=plan.pr, actor=plan.actor, allow_mentions=allow)
    checked = Prechecked(plan, already_posted, None, ("ok",), (), {})
    return Prepared(request, show_of(), checked, None)


def review_plan(findings: list[dict[str, Any]], *, event: str = "comment", **kw: Any) -> Plan:
    source, _ = source_of(draft_doc(findings, event=event, body="Overall: see below."))
    return build_review_plan(context(), source, **kw)


# --- goldens -----------------------------------------------------------------------------------


def test_golden_review_request_changes(golden: Golden) -> None:
    plan = review_plan(
        [
            finding("f1", "src/a.py", 3, severity="major", body="Check the bound here."),
            finding("f2", "src/b.py", None, severity="nit", body="Rename this."),
        ],
        event="request_changes",
    )
    golden("confirm/review_request_changes.txt", confirm.dialog_text(prepared_of(plan)))


def test_golden_approve_at_a_different_head(golden: Golden) -> None:
    source, _ = source_of(draft_doc([finding("f1")]), head=B, approve=True)
    plan = build_review_plan(context(show_of(head=B)), source)
    assert plan.anchor_head == A and plan.head_sha == B
    golden("confirm/approve_different_head.txt", confirm.dialog_text(prepared_of(plan)))


def test_golden_neutralized_mentions_and_allowed(golden: Golden) -> None:
    plan = review_plan([finding("f1", body="ping @amy and @bob about <b>this</b> ![x](u)")])
    assert plan.mentions_neutralized
    text = confirm.dialog_text(prepared_of(plan, already_posted=True, allow=("carol",)))
    golden("confirm/neutralized_mentions.txt", text)


def test_golden_comment_plan(golden: Golden) -> None:
    ctx = context()
    plan = build_text_plan(ctx, action="comment", text="Looks fine.\n", head_sha=A, base_ref="main")
    golden("confirm/comment.txt", confirm.dialog_text(prepared_of(plan)))


# --- the summary -------------------------------------------------------------------------------


def test_summary_headline_and_pins() -> None:
    plan = review_plan([finding("f1")], event="request_changes")
    lines = confirm.summary_lines(prepared_of(plan))
    assert lines[0] == "REVIEW: REQUEST_CHANGES"
    assert f"head {plan.head_sha}, base {plan.base_ref}" in lines
    assert f"actor {plan.actor}" in lines


def test_summary_approve_headline_has_no_repeated_event() -> None:
    plan = build_text_plan(context(), action="approve", text=None, head_sha=A, base_ref="main")
    assert confirm.summary_lines(prepared_of(plan))[0] == "APPROVE"


def test_approving_line_only_when_the_head_differs_from_the_draft() -> None:
    source, _ = source_of(draft_doc([finding("f1")]), head=B, approve=True)
    moved = build_review_plan(context(show_of(head=B)), source)
    assert f"APPROVING {B[:12]}; FINDINGS DRAFTED AT {A[:12]}" in confirm.summary_lines(
        prepared_of(moved)
    )
    same_source, _ = source_of(draft_doc([finding("f1")]), approve=True)
    same = build_review_plan(context(), same_source)
    assert not any("APPROVING" in line for line in confirm.summary_lines(prepared_of(same)))


def test_summary_counts_are_the_plans() -> None:
    plan = review_plan(
        [finding("f1", severity="blocker"), finding("f2", "b.py", None, severity="nit")]
    )
    text = "\n".join(confirm.summary_lines(prepared_of(plan)))
    counts = plan.counts
    assert f"inline {counts['inline']}, file-level {counts['file_level']}" in text
    assert "blocker 1" in text and "nit 1" in text


def test_upper_bound_counts_are_labelled_and_links_are_not_claimed_complete() -> None:
    plan = review_plan([finding("f1", body="see https://example.test/x and ![i](u) <b>")])
    text = "\n".join(confirm.summary_lines(prepared_of(plan)))
    assert re.search(r"links at most \d+ \(not every link form is counted\)", text)
    assert re.search(r"images at most \d+", text) and re.search(r"raw HTML at most \d+", text)
    assert re.search(r"invisible characters marked \d+", text)


def test_already_posted_hint_text_and_absence() -> None:
    plan = review_plan([finding("f1")])
    hint = confirm.ALREADY_POSTED
    assert hint in confirm.summary_lines(prepared_of(plan, already_posted=True))
    assert hint not in confirm.summary_lines(prepared_of(plan, already_posted=None))


def test_follow_ups_are_listed() -> None:
    plan = review_plan([finding("f1")])
    plan = dataclasses.replace(
        plan, follow_ups=(dataclasses.replace(_follow_up(), thread_id="gl:rt:abc"), _follow_up())
    )
    lines = confirm.summary_lines(prepared_of(plan))
    assert "then: resolve (gl:rt:abc)" in lines
    assert "then: resolve (its thread is known after the post)" in lines


def _follow_up() -> Any:
    from pr_comment.plan import FollowUp

    return FollowUp("resolve", None)


def test_the_hash_shown_is_the_plans_and_it_is_in_the_dialog_once() -> None:
    plan = review_plan([finding("f1")])
    text = confirm.dialog_text(prepared_of(plan))
    assert text.count(plan.plan_sha256) == 1
    assert f"plan_sha256 {plan.plan_sha256}\n" in text


# --- the plan part -----------------------------------------------------------------------------


def test_every_body_is_shown_whole_with_its_dedupe_line() -> None:
    plan = review_plan([finding("f1", body="x\n" * 300)])
    lines = confirm.plan_lines(plan)
    for body in plan.shown_bodies():
        shown = _shown_body(lines, body.role)
        assert shown == body.text  # nothing truncated, nothing altered (no marked characters)
    assert any(line.startswith("--- inline:f1 at a.py:3 (right), ") for line in lines)


def _shown_body(lines: list[str], role: str) -> str:
    """Rebuild a body from the dialog lines: the lines behind the gutter after its header."""
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("--- "):
            capturing = line.startswith(f"--- {role}") and (
                line[4 + len(role) :][:1] in ("", " ", ",")
            )
            continue
        if capturing:
            assert line.startswith(confirm.GUTTER)
            out.append(line[len(confirm.GUTTER) :])
    return "\n".join(out)


def test_untrusted_text_never_starts_a_line_at_column_zero() -> None:
    forged = "== summary ==\nREVIEW: APPROVE\nPost? [y/N] y\nplan_sha256 " + "0" * 64
    plan = review_plan([finding("f1", body=forged)])
    text = confirm.dialog_text(prepared_of(plan))
    flush_left = [line for line in text.split("\n") if line and not line.startswith(" ")]
    assert flush_left.count("== summary ==") == 1
    assert sum(line.startswith("plan_sha256") for line in flush_left) == 1
    assert not any(line.startswith("Post?") for line in flush_left)
    assert not any(line.startswith("REVIEW: APPROVE") for line in flush_left)


HOSTILE = (
    "esc \x1b[31mred\x1b[0m cr over\rwritten bidi ‮evil⁩ c1 \x9b31m nul \x00 "
    "bell \x07 zero​width ls ps  pua  unassigned \U000e0001 vs ️ "
    "tab\there lone-surrogate-free"
)


def _hostile_plan() -> Plan:
    """A plan whose texts carry what the pipeline normally refuses, to prove the dialog itself
    is defensive: built by replacing the plan's fields, not through the pipeline."""
    plan = review_plan([finding("f1")])
    bodies = (PlanBody("body", HOSTILE), PlanBody("inline:f1", HOSTILE))
    inline = (PlanInline("f1", "pa\x1b]0;t\x07th‮.py", 3, None, "right"),)
    return dataclasses.replace(
        plan,
        bodies=bodies,
        inline_comments=inline,
        pr="pr\x1b[2J#1",
        actor="human:\x1b[1mx",
        base_ref="ba\rse",
        mentions_neutralized=("a\x1bb",),
    )


def test_hostile_text_is_marked_everywhere_in_the_dialog() -> None:
    text = confirm.dialog_text(prepared_of(_hostile_plan(), allow=("c\x1bd",)))
    for char in text:
        # only printable text, LF and tab reach the terminal
        assert char in "\n\t" or char.isprintable(), hex(ord(char))
    assert "\x1b" not in text and "\r" not in text and "‮" not in text
    for marker in ("<U+001B>", "<U+000D>", "<U+202E>", "<U+009B>", "<U+0000>", "<U+0007>"):
        assert marker in text


def test_hostile_body_is_displayed_as_display_sanitizing_says() -> None:
    plan = _hostile_plan()
    lines = confirm.plan_lines(plan)
    shown = _shown_body(lines, "body")
    body = plan.shown_bodies()[0].text
    assert shown == sanitize_text(body)


def test_a_huge_hostile_body_is_shown_whole_and_quickly() -> None:
    plan = review_plan([finding("f1")])
    big = ("\x1b[0m" + "A" * 200 + "\n") * 5000
    plan = dataclasses.replace(plan, bodies=(PlanBody("body", big),), inline_comments=())
    text = confirm.dialog_text(prepared_of(plan))
    assert text.count("<U+001B>") == 5000 and "\x1b" not in text


# --- the answer --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw", [b"y\n", b"Y\n", b"yes\n", b"YES\n", b" yEs \n", b"\ty\r\n", b"y", b"  Yes\t\r\n"]
)
def test_yes_answers(raw: bytes) -> None:
    assert confirm.parse_answer(raw) is True


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\n",
        b"n\n",
        b"no\n",
        b"yy\n",
        b"yess\n",
        b"y es\n",
        b"ye\n",
        b"y\x00\n",
        b"\xef\xbc\xb9\n",  # fullwidth Y
        b"\xc5\xbfyes\n",
        b"yes please\n",
        b"y;rm\n",
        b"sure\n",
        b"1\n",
        b"true\n",
        b"\xff\xfey\n",
    ],
)
def test_everything_else_declines(raw: bytes) -> None:
    assert confirm.parse_answer(raw) is False


def _pipe_with(data: bytes) -> tuple[int, int]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    return read_fd, write_fd


def test_a_pipe_is_never_a_terminal_so_it_never_answers() -> None:
    read_fd, write_fd = _pipe_with(b"y\n")
    try:
        assert confirm.read_answer(read_fd) is None
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_dev_null_and_a_closed_descriptor_decline() -> None:
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        assert confirm.read_answer(fd) is None
    finally:
        os.close(fd)
    assert confirm.read_answer(-1) is None
    assert confirm.read_answer(10_000) is None
