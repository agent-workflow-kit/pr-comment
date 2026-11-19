"""The confirmation dialog's text and answer parsing (docs/interface.md, Confirmation).

The terminal behaviour (a real pty, signals, type-ahead) is in ``test_confirm_pty.py``.
"""

from __future__ import annotations

import dataclasses
import os
import pty
import re
import signal
import tty
from collections.abc import Callable
from typing import Any

import pytest

from pr_comment import confirm
from pr_comment.config import Mentions
from pr_comment.display import sanitize_text
from pr_comment.plan import Plan, PlanBody, PlanInline, build_review_plan, build_text_plan
from pr_comment.precheck import Prechecked
from pr_comment.prepare import Prepared, Request
from tests.posting import A, B, context, draft_doc, finding, settings, show_of, source_of

GOLDEN_WIDTH = 100  # wide enough that the attribution line is not wrapped
WIDE = 100_000  # no wrapping: bodies are rebuilt from their gutter lines

Golden = Callable[[str, str], None]


def prepared_of(
    plan: Plan, *, already_posted: bool | None = None, allow: tuple[str, ...] = ()
) -> Prepared:
    request = Request(command=plan.action, pr=plan.pr, actor=plan.actor, allow_mentions=allow)
    if allow:
        plan = dataclasses.replace(plan, mentions_allowed=allow)
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
    golden(
        "confirm/review_request_changes.txt", confirm.dialog_text(prepared_of(plan), GOLDEN_WIDTH)
    )


def test_golden_approve_at_a_different_head(golden: Golden) -> None:
    source, _ = source_of(draft_doc([finding("f1")]), head=B, approve=True)
    plan = build_review_plan(context(show_of(head=B)), source)
    assert plan.anchor_head == A and plan.head_sha == B
    golden(
        "confirm/approve_different_head.txt", confirm.dialog_text(prepared_of(plan), GOLDEN_WIDTH)
    )


def test_golden_neutralized_mentions_and_allowed(golden: Golden) -> None:
    plan = review_plan([finding("f1", body="ping @amy and @bob about <b>this</b> ![x](u)")])
    assert plan.mentions_neutralized
    text = confirm.dialog_text(
        prepared_of(plan, already_posted=True, allow=("carol",)), GOLDEN_WIDTH
    )
    golden("confirm/neutralized_mentions.txt", text)


def test_golden_comment_plan(golden: Golden) -> None:
    ctx = context()
    plan = build_text_plan(ctx, action="comment", text="Looks fine.\n", head_sha=A, base_ref="main")
    golden("confirm/comment.txt", confirm.dialog_text(prepared_of(plan), GOLDEN_WIDTH))


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


@pytest.mark.parametrize("event", ["comment", "request_changes"])
def test_no_approving_banner_for_a_non_approving_event_at_a_different_head(event: str) -> None:
    source, _ = source_of(draft_doc([finding("f1")], event=event), head=B)
    plan = build_review_plan(context(show_of(head=B)), source)
    assert plan.anchor_head == A and plan.head_sha == B
    assert not any("APPROVING" in line for line in confirm.summary_lines(prepared_of(plan)))


def _allowed_line(plan: Plan) -> list[str]:
    return [x for x in confirm.summary_lines(prepared_of(plan)) if x.startswith("mentions allowed")]


def test_mentions_allowed_lists_the_config_and_the_flag() -> None:
    both = settings(mentions=Mentions(policy="neutralize", allow=("cfg-team", "org/team")))
    ctx = context(repo_settings=both, allow_mentions=["flagged"])
    source, _ = source_of(draft_doc([finding("f1")]))
    plan = build_review_plan(ctx, source)
    assert _allowed_line(plan) == ["mentions allowed this run: cfg-team, org/team, flagged"]


def test_mentions_allowed_says_all_when_the_policy_allows() -> None:
    everything = settings(mentions=Mentions(policy="allow"))
    source, _ = source_of(draft_doc([finding("f1")]))
    plan = build_review_plan(context(repo_settings=everything), source)
    assert _allowed_line(plan) == ["mentions allowed this run: all (mentions.policy is allow)"]


def test_no_mentions_allowed_line_by_default() -> None:
    assert _allowed_line(review_plan([finding("f1")])) == []


def test_summary_counts_are_the_plans() -> None:
    plan = review_plan(
        [finding("f1", severity="blocker"), finding("f2", "b.py", None, severity="nit")]
    )
    text = "\n".join(confirm.summary_lines(prepared_of(plan)))
    counts = plan.counts
    assert f"inline {counts['inline']}, file-level {counts['file_level']}" in text
    assert "blocker 1" in text and "nit 1" in text


def test_upper_bound_counts_are_labelled_and_links_count_written_destinations() -> None:
    plan = review_plan([finding("f1", body="see https://example.test/x and ![i](u) <b>")])
    text = "\n".join(confirm.summary_lines(prepared_of(plan)))
    assert re.search(r"links at most [1-9]\d*", text)
    assert "not every link form" not in text
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


def test_only_the_body_carries_the_dedupe_line_not_an_inline_comment() -> None:
    plan = review_plan([finding("f1"), finding("f2", "b.py", 4)])
    shown = {b.role: b.text for b in plan.shown_bodies()}
    mark = f"<!-- forgectl:dedupe:{plan.dedupe_key} -->"
    assert shown["body"].endswith(mark)
    for role in ("inline:f1", "inline:f2"):
        assert mark not in shown[role]
    text = confirm.dialog_text(prepared_of(plan))
    assert text.count(mark) == 1


def test_every_body_is_shown_whole_with_its_dedupe_line() -> None:
    plan = review_plan([finding("f1", body="x\n" * 300)])
    lines = confirm.plan_lines(plan, WIDE)
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
    lines = confirm.plan_lines(plan, WIDE)
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


@pytest.mark.parametrize(
    "raw",
    [
        b"y\xc2\xa0\n",  # no-break space
        b"y\xe3\x80\x80\n",  # ideographic space
        b"\xc2\xa0y\n",
        b"y\xff\n",
        b"y\xef\xbf\xbd\n",
    ],
)
def test_non_ascii_whitespace_and_invalid_utf8_decline(raw: bytes) -> None:
    assert confirm.parse_answer(raw) is False


@pytest.mark.parametrize("raw", [b"yes\n", b" \tyes \r\n", b"\x0b\x0cYes\x0b\n"])
def test_ascii_whitespace_around_yes_accepts(raw: bytes) -> None:
    assert confirm.parse_answer(raw) is True


# --- reading from a real terminal -------------------------------------------------------------


def _raw_tty() -> tuple[int, int]:
    """A pty with the slave in raw mode (no line discipline): (master, slave)."""
    master, slave = pty.openpty()
    tty.setraw(slave)
    return master, slave


def _canonical_tty() -> tuple[int, int]:
    return pty.openpty()


def _close(*fds: int) -> None:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def test_only_the_first_line_is_the_answer() -> None:
    master, slave = _raw_tty()  # ICANON cleared: one read can hold several lines
    try:
        os.write(master, b"\ny\n")
        raw = confirm.read_answer(slave)
        assert raw == b"" and confirm.parse_answer(raw) is False
        os.write(master, b"y\nn\n")
        assert confirm.read_answer(slave) == b"y"
    finally:
        _close(master, slave)


@pytest.mark.parametrize(("size", "accepted"), [(255, True), (256, True), (257, False)])
def test_the_line_length_boundary_counts_the_newline(size: int, accepted: bool) -> None:
    master, slave = _raw_tty()
    try:
        os.write(master, b"y" + b" " * (size - 2) + b"\n")  # size bytes with the newline
        raw = confirm.read_answer(slave)
        assert (raw is not None and confirm.parse_answer(raw)) is accepted
    finally:
        _close(master, slave)


def test_a_valid_yes_padded_past_the_cap_declines() -> None:
    master, slave = _raw_tty()
    try:
        os.write(master, b"y" + b" " * 300 + b"\n")
        assert confirm.parse_answer(b"y" + b" " * 300 + b"\n") is True  # valid without the cap
        assert confirm.read_answer(slave) is None
    finally:
        _close(master, slave)


def test_eof_after_a_partial_line_declines() -> None:
    master, slave = _canonical_tty()
    try:
        os.write(master, b"y\x04\x04")  # "y" handed over without a newline, then EOF
        assert confirm.read_answer(slave) is None
    finally:
        _close(master, slave)


def test_flush_input_ignores_a_descriptor_that_is_not_a_terminal() -> None:
    read_fd, write_fd = os.pipe()
    try:
        confirm._flush_input(read_fd)  # termios.error (not a tty) must not escape
        confirm._flush_input(-1)
    finally:
        _close(read_fd, write_fd)


def test_flush_input_ignores_a_terminal_whose_master_is_gone() -> None:
    master, slave = _canonical_tty()
    _close(master)
    try:
        confirm._flush_input(slave)
    finally:
        _close(slave)


def test_confirm_declines_on_a_pipe_without_raising() -> None:
    read_fd, write_fd = os.pipe()
    out: list[str] = []
    try:
        assert (
            confirm.confirm(
                prepared_of(review_plan([finding("f1")])), stdin_fd=read_fd, write=out.append
            )
            is False
        )
    finally:
        _close(read_fd, write_fd)
    assert confirm.PROMPT in out


# --- signal handlers --------------------------------------------------------------------------

SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _sentinel(_signum: int, _frame: object) -> None:
    raise AssertionError("sentinel handler ran")


@pytest.fixture
def sentinels() -> Any:
    saved = {sig: signal.getsignal(sig) for sig in SIGNALS}
    for sig in SIGNALS:
        signal.signal(sig, _sentinel)
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def _assert_restored() -> None:
    for sig in SIGNALS:
        assert signal.getsignal(sig) is _sentinel, sig.name


@pytest.mark.usefixtures("sentinels")
@pytest.mark.parametrize("answer", [b"y\n", b"n\n"])
def test_handlers_are_restored_after_an_accept_and_a_decline(answer: bytes) -> None:
    master, slave = _canonical_tty()
    seen: list[bool] = []

    def write(text: str) -> None:
        seen.append(all(signal.getsignal(s) is not _sentinel for s in SIGNALS))
        if text == confirm.PROMPT:  # typed after the input flush, so it counts
            os.write(master, answer)

    try:
        confirm.confirm(prepared_of(review_plan([finding("f1")])), stdin_fd=slave, write=write)
    finally:
        _close(master, slave)
    assert seen and all(seen)  # the dialog's own handlers were in place while it ran
    _assert_restored()


@pytest.mark.usefixtures("sentinels")
def test_handlers_are_restored_when_the_dialog_raises() -> None:
    def boom(_text: str) -> None:
        raise RuntimeError("write failed")

    with pytest.raises(RuntimeError):
        confirm.confirm(prepared_of(review_plan([finding("f1")])), stdin_fd=-1, write=boom)
    _assert_restored()


@pytest.mark.usefixtures("sentinels")
@pytest.mark.parametrize("sig", SIGNALS)
def test_handlers_are_restored_after_an_interrupt(sig: signal.Signals) -> None:
    def interrupt(text: str) -> None:
        if text == confirm.PROMPT:
            signal.raise_signal(sig)

    with pytest.raises(confirm.Interrupted) as caught:
        confirm.confirm(prepared_of(review_plan([finding("f1")])), stdin_fd=-1, write=interrupt)
    assert caught.value.signum == sig
    _assert_restored()


# --- wrapping and encoding ----------------------------------------------------------------------


def _flush_left(text: str) -> list[str]:
    return [line for line in text.split("\n") if line and not line.startswith(" ")]


def test_a_forged_heading_at_the_wrap_point_never_starts_a_line() -> None:
    width = 60
    room = width - len(confirm.GUTTER) - 1
    forged = "x" * room + "== summary ==\nPost? [y/N] y"
    pad = "y" * (room - 5) + "plan_sha256 " + "0" * 64
    plan = review_plan([finding("f1", body=forged), finding("f2", "b.py", 5, body=pad)])
    text = confirm.dialog_text(prepared_of(plan), width)
    flush_left = _flush_left(text)
    assert flush_left.count("== summary ==") == 1
    assert sum(line.startswith("plan_sha256") for line in flush_left) == 1
    assert not any(line.startswith("Post?") for line in flush_left)
    body_lines = [line for line in text.split("\n") if "== summary ==" in line]
    assert any(line == confirm.GUTTER + "== summary ==" for line in body_lines)


def test_every_body_part_has_the_gutter_and_fits_the_width() -> None:
    width = 50
    plan = review_plan([finding("f1", body="word " * 200)])
    lines = confirm.plan_lines(plan, width)
    for line in lines[1:]:
        assert line.startswith(("---", confirm.GUTTER)), line
        assert len(line) < width
    rebuilt = "".join(
        line[len(confirm.GUTTER) :] for line in lines if line.startswith(confirm.GUTTER)
    )
    assert "word " * 200 in rebuilt.replace("\n", "")  # nothing dropped


def test_wide_characters_count_double_when_wrapping() -> None:
    width = 40
    plan = dataclasses.replace(
        review_plan([finding("f1")]), bodies=(PlanBody("body", "\u3042" * 100),), inline_comments=()
    )
    wide = [x for x in confirm.plan_lines(plan, width) if "\u3042" in x]
    assert len(wide) > 3
    for line in wide:
        assert line.startswith(confirm.GUTTER)
        assert 2 * len(line[len(confirm.GUTTER) :]) <= width


def test_an_unencodable_look_alike_is_shown_as_a_marker() -> None:
    plan = review_plan([finding("f1", body="a <b> c")])
    assert "\uff1c" in "\n".join(p.text for p in plan.bodies)  # neutralized to a look-alike
    text = confirm.dialog_text(prepared_of(plan), 80, "ascii")
    text.encode("ascii")  # everything is encodable
    assert "<U+FF1C>" in text and "\uff1c" not in text
    assert "\\u" not in text  # not a backslash escape


def test_ascii_dialog_wraps_after_marker_substitution() -> None:
    width = 60
    room = width - len(confirm.GUTTER) - 1
    body = "一" * 100 + "x" * (room - 3) + "== summary ==\n" + "Ａ" * 30
    plan = dataclasses.replace(
        review_plan([finding("f1")]), bodies=(PlanBody("body", body),), inline_comments=()
    )
    text = confirm.dialog_text(prepared_of(plan), width, "ascii")
    text.encode("ascii")
    lines = text.split("\n")
    start = lines.index("== plan ==")
    end = lines.index("== summary ==")
    for line in lines[start + 2 : end]:
        if line.startswith("---"):
            continue
        assert line.startswith(confirm.GUTTER), line
        assert len(line) <= width, line
    assert _flush_left(text).count("== summary ==") == 1


@pytest.mark.parametrize("width", [1, 5, 24])
def test_narrow_widths_still_wrap_with_progress(width: int) -> None:
    source = "abc def ghij klmno " * 6
    lines = confirm._gutter(source, width)
    assert lines
    assert all(line.startswith(confirm.GUTTER) for line in lines)
    assert "".join(line[len(confirm.GUTTER) :] for line in lines) == source
    if width >= len(confirm.GUTTER) + 1:
        assert all(len(line) <= width for line in lines)


def test_the_dialog_is_unchanged_on_a_utf8_stream() -> None:
    plan = review_plan([finding("f1", body="a <b> c")])
    assert confirm.dialog_text(prepared_of(plan), 80, "utf-8") == confirm.dialog_text(
        prepared_of(plan)
    )
