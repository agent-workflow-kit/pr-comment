"""Finding the GitLab review body thread (``src/pr_comment/gitlab_thread.py``, issue #36)."""

from __future__ import annotations

from typing import Any

import pytest

from pr_comment.forge import CommentsRead
from pr_comment.gitlab_thread import BodyThread, find_body_thread, marker_line
from tests import forgectl_harness as h

KEY = "prc:rv:01ARZ3NDEKTSV4RRFFQ69G5FAV"
MARK = marker_line(KEY)
T1 = "gl:d:" + "1" * 40
T2 = "gl:d:" + "2" * 40


def read(*threads: dict[str, Any]) -> CommentsRead:
    return CommentsRead.model_validate(h.comments_doc(h.GITLAB_PR, threads=threads))


def thread(tid: str, *bodies: str, first_id: int = 1, **kw: Any) -> dict[str, Any]:
    comments = [h.comment_doc(f"gl:n:{first_id + i}", b) for i, b in enumerate(bodies)]
    return h.thread_doc(tid, comments, **kw)


def find(doc: CommentsRead, *, ids: list[str] | None = None, **kw: Any) -> BodyThread | None:
    kw.setdefault("thread_ids", [T1])
    kw.setdefault("already_posted", False)
    return find_body_thread(doc, key=KEY, ids=ids or [], **kw)


def test_the_thread_of_the_review_whose_first_comment_ends_with_the_marker() -> None:
    found = find(read(thread(T1, f"text\n{MARK}"), thread(T2, "inline", first_id=9)))
    assert found == BodyThread(T1, True, False, None, None)


@pytest.mark.parametrize("body", [f"x\n{MARK}\n", f"x\n{MARK}  \n\n\n", f"x\r\n{MARK}\r\n", MARK])
def test_trailing_blank_lines_and_whitespace_do_not_hide_the_marker(body: str) -> None:
    assert find(read(thread(T1, body))) is not None


@pytest.mark.parametrize(
    "body",
    [
        f"{MARK}\nmore",  # not the last line
        f"x {MARK}",  # not alone on its line
        f"x\n {MARK}",  # leading whitespace is not forgectl's line
        f"x\n{MARK.upper()}",
        f"x\n{marker_line('prc:rv:' + '0' * 26)}",  # another run's key
        "",
    ],
)
def test_a_line_that_is_not_exactly_this_runs_marker_is_not_the_body_thread(body: str) -> None:
    assert find(read(thread(T1, body))) is None


def test_only_the_first_comment_counts() -> None:
    assert find(read(thread(T1, "plain", f"reply\n{MARK}"))) is None


def test_a_fresh_post_looks_only_at_the_threads_of_the_review() -> None:
    # a thread of an older review with the same marker text is not in thread_ids
    doc = read(thread(T2, f"x\n{MARK}"))
    assert find(doc, thread_ids=[T1]) is None
    assert find(doc, thread_ids=[T2]) is not None


def test_two_threads_with_the_marker_are_not_told_apart() -> None:
    doc = read(thread(T1, f"a\n{MARK}"), thread(T2, f"b\n{MARK}", first_id=5))
    assert find(doc, thread_ids=[T1, T2]) is None


def test_an_already_posted_review_is_found_by_the_comment_in_ids() -> None:
    doc = read(thread(T2, "inline", first_id=7), thread(T1, f"x\n{MARK}", first_id=1))
    found = find(doc, thread_ids=[], ids=["gl:n:1"], already_posted=True)
    assert found is not None and found.id == T1


def test_an_already_posted_review_needs_the_marker_too() -> None:
    doc = read(thread(T2, "inline", first_id=7))
    assert find(doc, thread_ids=[], ids=["gl:n:7"], already_posted=True) is None


def test_an_already_posted_review_ignores_thread_ids() -> None:
    doc = read(thread(T1, f"x\n{MARK}"))
    assert find(doc, thread_ids=[T1], ids=["gl:n:99"], already_posted=True) is None


def test_the_state_and_the_place_of_the_thread_are_reported() -> None:
    doc = read(thread(T1, f"x\n{MARK}", resolved=True))
    assert find(doc) == BodyThread(T1, True, True, None, None)
    doc = read(thread(T1, f"x\n{MARK}", resolvable=False))
    assert find(doc) == BodyThread(T1, False, False, None, None)


# --- the answer of ``resolve`` -------------------------------------------------------------------


def _resolve_read(document: dict[str, Any]) -> Any:
    from pr_comment.forge import read_resolve
    from tests.fake_gateway import FakeGateway

    return read_resolve(document, thread=T1, pr=h.GITLAB_PR, gateway=FakeGateway())


def test_a_real_resolve_answer_for_the_thread_is_read() -> None:
    read_ = _resolve_read(h.resolve_doc("resolve", T1, h.GITLAB_PR))
    assert read_.changed is True and read_.resolved is True
    assert _resolve_read(h.resolve_doc("resolve", T1, h.GITLAB_PR, changed=False)).changed is False


@pytest.mark.parametrize(
    "change",
    [
        {"thread_id": T2},
        {"action": "unresolve"},
        {"dry_run": True},
        {"resolved": None},
        {"resolved": False},
        {"pr": "gitlab.com/acme/widgets#8"},
        {"changed": "yes"},
        {"thread_id": "not a thread"},
    ],
)
def test_a_resolve_answer_that_is_not_this_threads_resolve_is_a_landed_mismatch(
    change: dict[str, Any],
) -> None:
    from pr_comment.lower import LowerFailure

    document = {**h.resolve_doc("resolve", T1, h.GITLAB_PR), **change}
    with pytest.raises(LowerFailure) as caught:
        _resolve_read(document)
    error = (caught.value.outcome.document or {})["error"]
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")
