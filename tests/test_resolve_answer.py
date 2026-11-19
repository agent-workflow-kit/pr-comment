"""The one strict check of forgectl's ``resolve``/``unresolve`` answer, pinned from both of its
callers: ``forge.read_resolve`` (the GitLab body thread follow-up) and ``threads._checked_answer``
(the resolve/unresolve orders). A change to the shared check must fail the tests of both."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from pr_comment.forge import ResolveRead, read_resolve
from pr_comment.hooks import ThreadTarget
from pr_comment.lower import LowerFailure
from pr_comment.threads import Item, ThreadRequest, ThreadRun, _checked_answer
from tests import forgectl_harness as h
from tests.fake_gateway import FakeGateway
from tests.posting import PR, show_of

ACTOR = "human:alice"
T1 = "gh:rt:PRRT_1"
T2 = "gh:rt:PRRT_2"

Caller = Callable[[Any], ResolveRead]


def _via_body_thread(document: Any) -> ResolveRead:
    return read_resolve(document, thread=T1, pr=PR, gateway=FakeGateway())


def _via_thread_orders(document: Any) -> ResolveRead:
    request = ThreadRequest("resolve", PR, ACTOR, (T1,))
    run = ThreadRun(request, show_of(), h.HEAD)
    return _checked_answer(
        document, request, Item(T1, ThreadTarget(T1), True), run.show, FakeGateway()
    )


CALLERS = [
    pytest.param(_via_body_thread, id="body-thread"),
    pytest.param(_via_thread_orders, id="orders"),
]

WRONG = [
    pytest.param(h.resolve_doc("resolve", T2), id="another-thread"),
    pytest.param(h.resolve_doc("unresolve", T1), id="another-action"),
    pytest.param({**h.resolve_doc("resolve", T1), "action": "unresolve"}, id="action-alone"),
    pytest.param(h.resolve_doc("resolve", T1, dry_run=True), id="dry-run"),
    pytest.param({**h.resolve_doc("resolve", T1), "resolved": False}, id="not-resolved"),
    pytest.param({**h.resolve_doc("resolve", T1), "resolved": None}, id="resolved-null"),
    pytest.param({**h.resolve_doc("resolve", T1), "pr": "github.com/acme/other#8"}, id="other-pr"),
    pytest.param({**h.resolve_doc("resolve", T1), "changed": "yes"}, id="changed-not-bool"),
    pytest.param({**h.resolve_doc("resolve", T1), "dry_run": 0}, id="dry-run-not-bool"),
    pytest.param({"pr": PR}, id="missing-fields"),
    pytest.param(None, id="not-a-document"),
    pytest.param([h.resolve_doc("resolve", T1)], id="a-list"),
]


@pytest.mark.parametrize("call", CALLERS)
def test_a_real_answer_for_the_asked_thread_is_accepted(call: Caller) -> None:
    read = call({**h.resolve_doc("resolve", T1), "extra": "ignored"})
    assert (read.pr, read.thread_id, read.resolved, read.changed) == (PR, T1, True, True)


@pytest.mark.parametrize("call", CALLERS)
def test_an_unchanged_answer_for_the_asked_thread_is_accepted(call: Caller) -> None:
    assert call(h.resolve_doc("resolve", T1, changed=False)).changed is False


@pytest.mark.parametrize("call", CALLERS)
@pytest.mark.parametrize("answer", WRONG)
def test_a_wrong_answer_is_a_mismatch_that_landed(call: Caller, answer: Any) -> None:
    with pytest.raises(LowerFailure) as caught:
        call(answer)
    error = caught.value.outcome.document["error"]  # type: ignore[index]
    assert error["code"] == "lower_contract_mismatch"
    assert (error["landed"], error["retry"]) == (True, "none")


def test_each_kind_of_mismatch_of_the_body_thread_keeps_its_own_message() -> None:
    def message(document: Any) -> str:
        with pytest.raises(LowerFailure) as caught:
            _via_body_thread(document)
        return caught.value.outcome.document["error"]["message"]  # type: ignore[index]

    assert "not the real run's result for this PR" in message(
        h.resolve_doc("resolve", T1, dry_run=True)
    )
    assert "another thread" in message(h.resolve_doc("resolve", T2))
    assert "not resolved" in message({**h.resolve_doc("resolve", T1), "resolved": False})
    assert "invalid or missing: /changed" in message(
        {**h.resolve_doc("resolve", T1), "changed": "x"}
    )


def test_the_orders_say_one_thing_for_every_mismatch() -> None:
    messages: set[str] = set()
    for answer in (
        h.resolve_doc("resolve", T2),
        {**h.resolve_doc("resolve", T1), "resolved": False},
    ):
        with pytest.raises(LowerFailure) as caught:
            _via_thread_orders(answer)
        messages.add(caught.value.outcome.document["error"]["message"])  # type: ignore[index]
    assert len(messages) == 1


def test_the_orders_ask_for_the_state_of_the_order_not_always_resolved() -> None:
    request = ThreadRequest("unresolve", PR, ACTOR, (T1,))
    run = ThreadRun(request, show_of(), h.HEAD)
    item = Item(T1, ThreadTarget(T1), True)
    read = _checked_answer(h.resolve_doc("unresolve", T1), request, item, run.show, FakeGateway())
    assert read.resolved is False
    with pytest.raises(LowerFailure):
        _checked_answer(h.resolve_doc("resolve", T1), request, item, run.show, FakeGateway())


def test_the_body_thread_reads_the_pr_as_forgectl_does_but_the_orders_exactly() -> None:
    same_pr_other_spelling = {
        **h.resolve_doc("resolve", T1),
        "pr": PR.replace("github.com", "www.github.com", 1),
    }
    assert _via_body_thread(same_pr_other_spelling).thread_id == T1
    with pytest.raises(LowerFailure):
        _via_thread_orders(same_pr_other_spelling)
