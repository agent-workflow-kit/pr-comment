"""The forgectl reads (``src/pr_comment/forge.py``): good answers from the pinned fake, hostile
documents, error lowering per command (docs/interface.md: forgectl edge, Error envelope)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pr_comment.forge import Forge
from pr_comment.gateway import ForgectlResult, ProcessGateway
from pr_comment.grammars import GrammarError
from pr_comment.lower import LowerFailure
from pr_comment.output import check_outcome
from tests import forgectl_harness as h
from tests.fake_gateway import PINNED_LOWER_VERSION, FakeGateway, PinnedGateway, ok
from tests.fakes import Tracker
from tests.forgectl_harness import FakeForgectl
from tests.schema_harness import validate

PR = h.PR
HEAD, OTHER, BASE = h.HEAD, h.OTHER_HEAD, h.BASE_SHA
GATEWAY_VERSION = PINNED_LOWER_VERSION


def raw(subcommand: str, stdout: bytes, exit_code: int = 0) -> ForgectlResult:
    return ForgectlResult(command=subcommand, write=False, exit_code=exit_code, stdout=stdout)


def failing(subcommand: str, exit_code: int, envelope: Any) -> ForgectlResult:
    return raw(subcommand, json.dumps(envelope).encode(), exit_code)


def error_envelope(code: str, message: str = "m", **extra: Any) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, **extra}}


def forge_of(*replies: Any, pinned: bool = False) -> tuple[Forge, FakeGateway]:
    gateway: FakeGateway = (
        PinnedGateway(list(replies)) if pinned else FakeGateway(list(replies), GATEWAY_VERSION)
    )
    return Forge(gateway), gateway


def mismatch_of(call: Callable[[Forge], object], *replies: Any) -> dict[str, Any]:
    """Run a read that must end in ``lower_contract_mismatch`` and return its error."""
    forge, _ = forge_of(*replies)
    with pytest.raises(LowerFailure) as caught:
        call(forge)
    outcome = check_outcome(caught.value.outcome)
    assert outcome.exit_code == 12
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    error = outcome.document["error"]
    assert error["code"] == "lower_contract_mismatch"
    assert error["landed"] is False and error["retry"] == "same_command"
    assert error["details"]["lower_version"] == GATEWAY_VERSION
    return error  # type: ignore[no-any-return]


def failure_of(call: Callable[[Forge], object], *replies: Any) -> Any:
    forge, _ = forge_of(*replies)
    with pytest.raises(LowerFailure) as caught:
        call(forge)
    outcome = check_outcome(caught.value.outcome)
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    return outcome


def without(document: dict[str, Any], dotted: str) -> dict[str, Any]:
    doc = copy.deepcopy(document)
    target: Any = doc
    *parents, last = dotted.split(".")
    for part in parents:
        target = target[part]
    del target[last]
    return doc


def with_(document: dict[str, Any], dotted: str, value: Any) -> dict[str, Any]:
    doc = copy.deepcopy(document)
    target: Any = doc
    *parents, last = dotted.split(".")
    for part in parents:
        target = target[int(part)] if part.isdigit() else target[part]
    if last.isdigit():
        target[int(last)] = value
    else:
        target[last] = value
    return doc


# --- show ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["github", "gitlab"])
def test_show_reads_the_canonical_identity(backend: str) -> None:
    pr = PR if backend == "github" else h.GITLAB_PR
    forge, gateway = forge_of(ok("show", h.show_doc(pr, backend=backend)), pinned=True)
    url = (
        "https://github.com/acme/widgets/pull/7"
        if backend == "github"
        else "https://gitlab.com/acme/widgets/-/merge_requests/7"
    )
    shown = forge.show(url)  # a URL: forgectl parses it, pr-comment does not
    assert shown.pr == pr and shown.backend == backend and shown.state == "open"
    assert shown.repo == pr.rsplit("#", 1)[0] and shown.host == shown.repo.split("/")[0]
    assert shown.pr_id and shown.head_sha == HEAD
    assert shown.base_ref == "main" and shown.base_sha == BASE
    assert [(c.subcommand, c.args) for c in gateway.calls] == [("show", (url,))]


def test_show_with_a_canonical_argument_needs_the_same_host_and_number() -> None:
    renamed = h.show_doc("github.com/acme/renamed#7")
    assert forge_of(ok("show", renamed))[0].show(PR).repo == "github.com/acme/renamed"  # a rename
    mismatch_of(lambda f: f.show(PR), ok("show", h.show_doc("gitlab.com/acme/widgets#7")))
    mismatch_of(lambda f: f.show(PR), ok("show", h.show_doc("github.com/acme/widgets#8")))


def test_show_compares_a_mixed_case_host_and_forgectls_normal_forms() -> None:
    forge_of(ok("show", h.show_doc()))[0].show("GitHub.com/acme/widgets#7")
    forge_of(ok("show", h.show_doc()))[0].show("www.github.com/acme/widgets#7")
    forge_of(ok("show", h.show_doc()))[0].show("github.com:443/acme/widgets#7")
    mismatch_of(
        lambda f: f.show("GitHub.com/acme/widgets#7"),
        ok("show", h.show_doc("gitlab.com/acme/widgets#7")),
    )


def test_show_with_a_url_argument_skips_the_host_and_number_check() -> None:
    forge_of(ok("show", h.show_doc()))[0].show("https://other.example/acme/widgets/pull/9")


def test_a_mixed_case_pr_argument_is_still_compared_on_the_other_reads() -> None:
    mismatch_of(
        lambda f: f.diff("GitHub.com/acme/widgets#7"),
        ok("diff", h.diff_doc("gitlab.com/acme/widgets#7")),
    )


_READS: list[tuple[str, Callable[[Forge, str], object], Callable[[str], Any]]] = [
    ("diff", lambda f, pr: f.diff(pr), lambda pr: h.diff_doc(pr)),
    ("files", lambda f, pr: f.files(pr), lambda pr: h.files_doc(pr)),
    ("comments", lambda f, pr: f.comments(pr), lambda pr: h.comments_doc(pr)),
    (
        "compare",
        lambda f, pr: f.compare(pr, HEAD, OTHER),
        lambda pr: h.compare_doc(pr, from_sha=HEAD, to_sha=OTHER),
    ),
]
_READ_IDS = [name for name, _, _ in _READS]


@pytest.mark.parametrize(("name", "call", "doc"), _READS, ids=_READ_IDS)
@pytest.mark.parametrize(
    "asked",
    [
        "github.com:443/acme/widgets#7",
        "www.github.com/acme/widgets#7",
        "GitHub.com/acme/renamed-from#7",
    ],
)
def test_reads_accept_the_canonical_echo_of_a_normalized_or_renamed_argument(
    name: str, call: Callable[[Forge, str], object], doc: Callable[[str], Any], asked: str
) -> None:
    call(forge_of(ok(name, doc(PR)))[0], asked)


@pytest.mark.parametrize(("name", "call", "doc"), _READS, ids=_READ_IDS)
@pytest.mark.parametrize("echoed", ["gitlab.com/acme/widgets#7", "github.com/acme/widgets#8"])
def test_reads_reject_an_echo_for_another_host_or_number(
    name: str, call: Callable[[Forge, str], object], doc: Callable[[str], Any], echoed: str
) -> None:
    mismatch_of(lambda f: call(f, "www.github.com/acme/widgets#7"), ok(name, doc(echoed)))


def test_show_accepts_unknown_fields_and_a_null_base_sha() -> None:
    doc = h.show_doc()
    doc["something_new"] = {"nested": [1, 2]}
    doc["head"]["extra"] = 1
    doc["base"]["sha"] = None
    shown = forge_of(ok("show", doc))[0].show(PR)
    assert shown.base_sha is None


def test_show_without_head_sha_is_a_contract_mismatch() -> None:
    doc = without(h.show_doc(), "head.sha")
    error = mismatch_of(lambda f: f.show(PR), ok("show", doc))
    assert error["details"]["command"] == "show"
    assert "/head/sha" in error["message"]


@pytest.mark.parametrize(
    "path",
    ["pr", "pr_id", "repo", "number", "backend", "state", "head", "base", "base.ref", "base.sha"],
)
def test_show_missing_a_used_field_is_a_mismatch(path: str) -> None:
    mismatch_of(lambda f: f.show(PR), ok("show", without(h.show_doc(), path)))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("head.sha", "A" * 40),  # uppercase: forgectl prints lowercase
        ("head.sha", "a" * 39),
        ("head.sha", "a" * 41),
        ("head.sha", 12),
        ("head.sha", None),
        ("head.sha", ["a" * 40]),
        ("base.sha", "xyz"),
        ("pr", "github.com/acme/widgets#07"),
        ("pr", "github.com/acme/widgets#"),
        ("pr", "GitHub.com/acme/widgets#7"),
        ("pr", "github.com/acme/widgets#7\n"),
        ("pr", 7),
        ("pr", "github.com/acme/other#7"),  # not repo#number
        ("repo", "github.com/acme/widgets/"),
        ("repo", "github.com/../widgets"),
        ("repo", ""),
        ("repo", "github.com/acme/wid gets"),
        ("number", "7"),
        ("number", 7.0),
        ("number", True),
        ("number", 0),
        ("number", 10**30),
        ("number", float("1e400")),
        ("pr_id", ""),
        ("pr_id", "x" * 129),
        ("pr_id", "id with space"),
        ("pr_id", "PR_kwé"),
        ("backend", "bitbucket"),
        ("backend", "GitHub"),
        ("backend", None),
        ("state", "OPEN"),
        ("state", "draft"),
        ("state", ["open"]),
        ("base.ref", ""),
        ("base.ref", "x" * 1025),
        ("base.ref", "x\ny"),
        ("base.ref", "x\x00y"),
        ("base.ref", "x‮y"),
        ("base.ref", 1),
        ("base", "main"),
        ("head", None),
        ("head", []),
    ],
)
def test_show_with_a_bad_value_is_a_mismatch(path: str, value: Any) -> None:
    doc = with_(h.show_doc(), path, value)
    # the fake would refuse a document like this (the pinned schema), so the gateway is lax
    mismatch_of(lambda f: f.show(PR), ok("show", doc))


@pytest.mark.parametrize("ref", ["refs/heads/x", "-x", "HEAD", "a..b", "<U+202E>x", "x" * 1024])
def test_show_keeps_a_base_ref_git_allows_and_check_branch_judges_the_forwarding(ref: str) -> None:
    from pr_comment.grammars import check_branch

    shown = forge_of(ok("show", with_(h.show_doc(), "base.ref", ref)))[0].show(PR)
    assert shown.base_ref == ref
    try:
        expected = check_branch(ref)
    except GrammarError:
        with pytest.raises(GrammarError):
            shown.expected_base()
    else:
        assert shown.expected_base() == expected


def test_show_with_an_nan_or_infinity_literal_is_a_mismatch() -> None:
    text = json.dumps(h.show_doc()).replace('"number": 7', '"number": NaN')
    mismatch_of(lambda f: f.show(PR), raw("show", text.encode()))
    text = json.dumps(h.show_doc()).replace('"number": 7', '"number": Infinity')
    mismatch_of(lambda f: f.show(PR), raw("show", text.encode()))


def test_show_with_a_huge_integer_literal_is_a_mismatch() -> None:
    text = json.dumps(h.show_doc()).replace('"number": 7', '"number": ' + "9" * 5000)
    mismatch_of(lambda f: f.show(PR), raw("show", text.encode()))


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"null",
        b"[]",
        b'"show"',
        b"7",
        b"{",
        b"\xff\xfe",
        b'{"pr": "a", "pr": "b"}',
        b"[" * 100000 + b"]" * 100000,
        b'{"head": {"sha": "' + b"a" * 40 + b'"}}',
    ],
)
def test_show_with_output_that_is_not_the_document_is_a_mismatch(stdout: bytes) -> None:
    mismatch_of(lambda f: f.show(PR), raw("show", stdout))


def test_show_with_a_lone_surrogate_in_a_used_text_field_is_a_mismatch() -> None:
    doc = with_(h.show_doc(), "base.ref", "ma\ud800in")
    forge, _ = forge_of(
        ForgectlResult(
            command="show",
            write=False,
            exit_code=0,
            stdout=json.dumps(doc, ensure_ascii=True).encode(),
        )
    )
    with pytest.raises(LowerFailure):
        forge.show(PR)


def test_show_ignores_text_it_does_not_use_even_when_hostile() -> None:
    doc = h.show_doc()
    doc["title"] = "\ud800" * 10
    doc["body"] = 7
    doc["labels"] = "not a list"
    forge_of(
        ForgectlResult(
            command="show",
            write=False,
            exit_code=0,
            stdout=json.dumps(doc, ensure_ascii=True).encode(),
        )
    )[0].show(PR)


def test_show_rejects_an_argument_that_is_not_a_pr_before_any_call() -> None:
    forge, gateway = forge_of()
    for bad in ("", "-x", "a b#1", "plain", "x#1\n"):
        with pytest.raises(GrammarError):
            forge.show(bad)
    assert gateway.calls == []


# --- diff ----------------------------------------------------------------------------------------

DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"


def test_diff_reads_heads_and_text() -> None:
    forge, gateway = forge_of(ok("diff", h.diff_doc(diff=DIFF)), pinned=True)
    read = forge.diff(PR, expect_head=HEAD)
    assert read.head_sha == HEAD and read.merge_base_sha == BASE and read.diff == DIFF
    assert gateway.calls[0].args == (PR,)


def test_diff_allows_null_shas() -> None:
    doc = h.diff_doc()
    doc["base_sha"] = None
    doc["merge_base_sha"] = None
    read = forge_of(ok("diff", doc))[0].diff(PR)
    assert read.base_sha is None and read.merge_base_sha is None


def test_diff_with_another_head_is_stale_head() -> None:
    outcome = failure_of(
        lambda f: f.diff(PR, expect_head=OTHER), ok("diff", h.diff_doc(head_sha=HEAD))
    )
    assert outcome.exit_code == 20
    error = outcome.document["error"]
    assert error["reason"] == "stale_head" and error["details"] == {"head_sha": HEAD}


def test_diff_for_another_pr_is_a_mismatch() -> None:
    mismatch_of(lambda f: f.diff(PR), ok("diff", h.diff_doc("github.com/acme/widgets#8")))


def test_diff_for_a_url_argument_does_not_check_the_pr_it_echoes() -> None:
    forge_of(ok("diff", h.diff_doc()))[0].diff("https://github.com/acme/widgets/pull/7")


@pytest.mark.parametrize("path", ["pr", "head_sha", "base_sha", "merge_base_sha", "diff"])
def test_diff_missing_a_field_is_a_mismatch(path: str) -> None:
    mismatch_of(lambda f: f.diff(PR), ok("diff", without(h.diff_doc(), path)))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("diff", None),
        ("diff", 5),
        ("diff", ["x"]),
        ("head_sha", "short"),
        ("merge_base_sha", 0),
        ("base_sha", "G" * 40),
    ],
)
def test_diff_with_a_bad_value_is_a_mismatch(path: str, value: Any) -> None:
    mismatch_of(lambda f: f.diff(PR), ok("diff", with_(h.diff_doc(), path, value)))


def test_diff_with_a_lone_surrogate_in_the_text_is_a_mismatch() -> None:
    doc = h.diff_doc(diff="x\ud800y")
    mismatch_of(
        lambda f: f.diff(PR),
        ForgectlResult(
            command="diff",
            write=False,
            exit_code=0,
            stdout=json.dumps(doc, ensure_ascii=True).encode(),
        ),
    )


def test_diff_text_is_capped_at_forgectls_stdout_cap_not_the_body_cap() -> None:
    from pr_comment.forge import MAX_DIFF_BYTES, MAX_TEXT_BYTES
    from pr_comment.gateway import MAX_STDOUT_BYTES

    assert MAX_DIFF_BYTES == MAX_STDOUT_BYTES == 32 * 1024 * 1024 and MAX_TEXT_BYTES == 4 * 1024**2
    forge_of(ok("diff", h.diff_doc(diff="y" * (MAX_TEXT_BYTES + 1))))[0].diff(PR)
    forge_of(ok("diff", h.diff_doc(diff="y" * MAX_DIFF_BYTES)))[0].diff(PR)
    mismatch_of(lambda f: f.diff(PR), ok("diff", h.diff_doc(diff="y" * (MAX_DIFF_BYTES + 1))))


def test_compare_text_is_capped_at_forgectls_stdout_cap() -> None:
    from pr_comment.forge import MAX_DIFF_BYTES, MAX_TEXT_BYTES

    big = h.compare_doc(from_sha=HEAD, to_sha=OTHER, diff="y" * (MAX_TEXT_BYTES + 1))
    forge_of(ok("compare", big))[0].compare(PR, HEAD, OTHER)
    huge = h.compare_doc(from_sha=HEAD, to_sha=OTHER, diff="y" * (MAX_DIFF_BYTES + 1))
    mismatch_of(lambda f: f.compare(PR, HEAD, OTHER), ok("compare", huge))


def test_comment_bodies_stay_capped_at_4_mib() -> None:
    from pr_comment.forge import MAX_TEXT_BYTES

    doc = h.comments_doc(threads=[a_thread(comments=[a_comment(body="y" * (MAX_TEXT_BYTES + 1))])])
    mismatch_of(lambda f: f.comments(PR), ok("comments", doc))


def test_diff_expect_head_is_lowered_and_junk_is_a_grammar_error_before_any_call() -> None:
    forge, gateway = forge_of(ok("diff", h.diff_doc(head_sha=HEAD)))
    assert forge.diff(PR, expect_head=HEAD.upper()).head_sha == HEAD
    forge, gateway = forge_of()
    for bad in ("", "abc", "g" * 40, "--x"):
        with pytest.raises(GrammarError):
            forge.diff(PR, expect_head=bad)
    assert gateway.calls == []


# --- compare -------------------------------------------------------------------------------------


def test_compare_ahead_with_a_diff() -> None:
    forge, gateway = forge_of(
        ok("compare", h.compare_doc(from_sha=HEAD, to_sha=OTHER, diff=DIFF)), pinned=True
    )
    read = forge.compare(PR, HEAD.upper(), OTHER)
    assert read.status == "ahead" and read.diff == DIFF and read.to_sha == OTHER
    assert gateway.calls[0].args == (PR, f"--from={HEAD}", f"--to={OTHER}")


@pytest.mark.parametrize("status", ["behind", "diverged", "unavailable"])
def test_compare_without_a_diff_statuses_have_a_null_diff(status: str) -> None:
    doc = h.compare_doc(status=status, diff=None)
    read = forge_of(ok("compare", doc), pinned=True)[0].compare(PR, HEAD, OTHER)
    assert read.status == status and read.diff is None


def test_compare_identical_has_an_empty_diff() -> None:
    doc = h.compare_doc(status="identical", diff="")
    assert forge_of(ok("compare", doc))[0].compare(PR, HEAD, OTHER).diff == ""


@pytest.mark.parametrize(
    "doc",
    [
        h.compare_doc(status="ahead", diff=None),  # an ahead comparison always has a diff
        h.compare_doc(status="behind", diff="x"),
        h.compare_doc(status="diverged", diff=""),
        h.compare_doc(status="identical", diff="x"),
        h.compare_doc(status="identical", diff=None),
        h.compare_doc(status="ahead", include_diff=False),  # --no-diff was never passed
        h.compare_doc(status="sideways", diff=""),
        h.compare_doc(status="AHEAD", diff=""),
        h.compare_doc(status=None, diff=""),  # type: ignore[arg-type]
        h.compare_doc(from_sha=OTHER),  # not the commit asked for
        h.compare_doc(from_sha="x" * 40),
        h.compare_doc(diff=3),  # type: ignore[arg-type]
        h.compare_doc("github.com/acme/widgets#8"),
    ],
)
def test_compare_with_an_answer_that_does_not_fit_is_a_mismatch(doc: dict[str, Any]) -> None:
    mismatch_of(lambda f: f.compare(PR, HEAD, OTHER), ok("compare", doc))


def test_compare_with_another_to_sha_is_stale_head_with_the_sha_it_read() -> None:
    doc = h.compare_doc(from_sha=HEAD, to_sha=BASE)
    outcome = failure_of(lambda f: f.compare(PR, HEAD, OTHER), ok("compare", doc))
    assert outcome.exit_code == 20
    assert outcome.document["error"]["details"] == {"head_sha": BASE}


def test_compare_rejects_a_sha_argument_before_any_call() -> None:
    forge, gateway = forge_of()
    for bad in ("", "abc", "--from=x", "g" * 40):
        with pytest.raises(GrammarError):
            forge.compare(PR, bad, OTHER)
    assert gateway.calls == []


# --- files ---------------------------------------------------------------------------------------


def a_file(
    path: str = "a.py", status: str = "modified", previous: str | None = None
) -> dict[str, Any]:
    return {
        "path": path,
        "previous_path": previous,
        "status": status,
        "additions": 1,
        "deletions": 0,
    }


def test_files_reads_paths_statuses_and_completeness() -> None:
    files = [a_file("a.py"), a_file("b.py", "renamed", "old.py"), a_file("c<U+202E>.py", "added")]
    doc = h.files_doc(files=files)
    forge, gateway = forge_of(ok("files", doc), pinned=True)
    read = forge.files(PR)
    assert [f.path for f in read.files] == ["a.py", "b.py", "c<U+202E>.py"]
    assert read.files[1].previous_path == "old.py" and read.files_complete is True
    assert read.changed_files == 3 and read.head_sha == HEAD
    assert gateway.calls[0].args == (PR,)


def test_files_accepts_an_incomplete_list_and_a_null_count() -> None:
    doc = h.files_doc()
    doc["files_complete"] = False
    doc["changed_files"] = None
    read = forge_of(ok("files", doc))[0].files(PR)
    assert read.files_complete is False and read.changed_files is None


def test_files_accepts_a_symlink_type_change_as_deleted_then_added() -> None:
    files = [a_file("l", "deleted"), a_file("l", "added"), a_file("b.py")]
    read = forge_of(ok("files", h.files_doc(files=files)))[0].files(PR)
    assert [(f.path, f.status) for f in read.files][:2] == [("l", "deleted"), ("l", "added")]


@pytest.mark.parametrize(
    "files",
    [
        [a_file("l", "added"), a_file("l", "deleted")],  # the wrong order
        [a_file("l", "deleted"), a_file("l", "added"), a_file("l", "added")],  # three times
        [a_file("l", "deleted"), a_file("l", "deleted")],
        [a_file("l", "added"), a_file("l", "added")],
        [a_file("l", "modified"), a_file("l", "added")],
    ],
)
def test_files_with_any_other_repeated_path_is_a_mismatch(files: Any) -> None:
    mismatch_of(lambda f: f.files(PR), ok("files", h.files_doc(files=files)))


def test_files_complete_with_a_count_that_is_not_the_list_length_is_a_mismatch() -> None:
    doc = h.files_doc()
    doc["changed_files"] = 5
    mismatch_of(lambda f: f.files(PR), ok("files", doc))


def test_files_incomplete_may_have_any_count() -> None:
    doc = h.files_doc()
    doc["files_complete"] = False
    doc["changed_files"] = 5
    assert forge_of(ok("files", doc))[0].files(PR).changed_files == 5


def test_files_with_unicode_paths_keeps_them() -> None:
    doc = h.files_doc(files=[a_file("café/日本.py")])
    assert forge_of(ok("files", doc))[0].files(PR).files[0].path == "café/日本.py"


@pytest.mark.parametrize(
    "files",
    [
        [a_file("a.py"), a_file("a.py")],  # a duplicate path
        [a_file("")],
        [a_file("x\ny")],
        [a_file("x\ry")],
        [a_file("x\x0by")],
        [a_file("x\x0cy")],
        [a_file("x\x85y")],
        [a_file("x\u2028y")],
        [a_file("x\u2029y")],
        [a_file("x\x00y")],
        [a_file("a.py", "renamed", None)],  # renamed without previous_path
        [a_file("a.py", "modified", "old.py")],  # previous_path without rename
        [a_file("a.py", "moved")],
        [a_file("a.py", "renamed", "")],
        [a_file("p" * 70000)],
        [{**a_file(), "path": 5}],
        [{**a_file(), "path": None}],
        ["a.py"],
        [None],
        "a.py",
    ],
)
def test_files_with_a_bad_entry_is_a_mismatch(files: Any) -> None:
    doc = h.files_doc()
    doc["files"] = files
    mismatch_of(lambda f: f.files(PR), ok("files", doc))


def test_files_with_a_lone_surrogate_path_is_a_mismatch() -> None:
    doc = h.files_doc(files=[a_file("a\udc80.py")])
    mismatch_of(
        lambda f: f.files(PR),
        ForgectlResult(
            command="files",
            write=False,
            exit_code=0,
            stdout=json.dumps(doc, ensure_ascii=True).encode(),
        ),
    )


def test_files_with_more_entries_than_the_cap_is_a_mismatch() -> None:
    entries = [a_file(f"f{i}.py") for i in range(100_001)]
    doc = h.files_doc(files=entries)
    mismatch_of(lambda f: f.files(PR), ok("files", doc))


@pytest.mark.parametrize("path", ["pr", "head_sha", "files", "files_complete", "changed_files"])
def test_files_missing_a_field_is_a_mismatch(path: str) -> None:
    mismatch_of(lambda f: f.files(PR), ok("files", without(h.files_doc(), path)))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("files_complete", "true"),
        ("files_complete", 1),
        ("files_complete", None),
        ("changed_files", -1),
        ("changed_files", "3"),
        ("changed_files", 2**40),
        ("changed_files", 1.5),
        ("head_sha", "A" * 40),
    ],
)
def test_files_with_a_bad_value_is_a_mismatch(path: str, value: Any) -> None:
    mismatch_of(lambda f: f.files(PR), ok("files", with_(h.files_doc(), path, value)))


# --- comments ------------------------------------------------------------------------------------

THREAD_GH = "gh:rt:PRRT_abc"
THREAD_GL = "gl:d:" + "0" * 40


def a_comment(cid: str = "gh:rc:C1", body: str = "hello", **extra: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "author": {"login": "octo", "id": "1", "is_bot": False},
        "body": body,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "minimized": False,
        "reply_to": None,
        "review_id": None,
        **extra,
    }


def a_thread(
    tid: str = THREAD_GH, comments: list[dict[str, Any]] | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": tid,
        "resolvable": True,
        "resolved": False,
        "outdated": False,
        "path": "a.py",
        "line": 3,
        "side": "right",
        "start_line": None,
        "commit_sha": HEAD,
        "comments": comments if comments is not None else [a_comment()],
        **extra,
    }


def test_comments_reads_threads_and_comments() -> None:
    threads = [
        a_thread(),
        a_thread(
            THREAD_GL,
            [a_comment("gl:n:5", "first\n<!-- pr-comment:v1 -->", review_id="gh:r:R1")],
            resolvable=False,
            resolved=False,
            line=None,
            path=None,
            side=None,
            outdated=None,
            commit_sha=None,
        ),
    ]
    forge, gateway = forge_of(ok("comments", h.comments_doc(threads=threads)), pinned=True)
    read = forge.comments(PR)
    assert read.complete is True and read.head_sha == HEAD
    first, second = read.threads
    assert first.id == THREAD_GH and first.resolvable and not first.resolved
    assert first.comments[0].author.login == "octo" and first.comments[0].author.id == "1"
    assert second.id == THREAD_GL and second.path is None and second.outdated is None
    assert second.comments[0].review_id == "gh:r:R1"
    assert second.comments[0].body.endswith("<!-- pr-comment:v1 -->")
    assert gateway.calls[0].args == (PR,)


def test_comments_accepts_a_general_comment_as_a_one_comment_pseudo_thread() -> None:
    # forgectl lists a GitHub issue comment as a thread that shares its id with its one comment
    thread = a_thread(
        "gh:ic:IC_1",
        [a_comment("gh:ic:IC_1")],
        resolvable=False,
        path=None,
        line=None,
        side=None,
        outdated=None,
        commit_sha=None,
    )
    read = forge_of(ok("comments", h.comments_doc(threads=[thread])))[0].comments(PR)
    assert read.threads[0].id == "gh:ic:IC_1" and read.threads[0].comments[0].id == "gh:ic:IC_1"


def test_comments_accepts_null_authors_and_an_incomplete_listing() -> None:
    comment = a_comment()
    comment["author"] = {"login": None, "id": None, "is_bot": None}
    doc = h.comments_doc(threads=[a_thread(comments=[comment])])
    doc["complete"] = False
    read = forge_of(ok("comments", doc))[0].comments(PR)
    assert read.complete is False and read.threads[0].comments[0].author.id is None


@pytest.mark.parametrize(
    "thread",
    [
        a_thread("gh:rt:"),
        a_thread("gh:rt:has space"),
        a_thread("gl:d:" + "G" * 40),
        a_thread("gl:d:" + "0" * 39),
        a_thread("gl:n:5"),
        a_thread(7),  # type: ignore[arg-type]
        a_thread(comments=[]),
        a_thread(comments=[a_comment("gh:xx:1")]),
        a_thread(comments=[a_comment("")]),
        a_thread(comments=[a_comment("gh:rc:C1"), a_comment("gh:rc:C1")]),  # duplicate id
        a_thread(comments=[{**a_comment(), "body": None}]),
        a_thread(comments=[{**a_comment(), "body": 5}]),
        a_thread(comments=[{**a_comment(), "body": "x\x00y"}]),
        a_thread(comments=[{**a_comment(), "author": None}]),
        a_thread(comments=[{**a_comment(), "author": {"login": 1, "id": None, "is_bot": None}}]),
        a_thread(
            comments=[{**a_comment(), "author": {"login": "a\nb", "id": None, "is_bot": None}}]
        ),
        a_thread(comments=[{**a_comment(), "author": {"login": "a", "id": "x y", "is_bot": None}}]),
        a_thread(comments=[{**a_comment(), "author": {"login": "a", "id": "1", "is_bot": "no"}}]),
        a_thread(comments=[{**a_comment(), "review_id": "gh:zz:1"}]),
        a_thread(resolvable=False, resolved=True),
        a_thread(resolvable="yes"),
        a_thread(resolved=None),
        a_thread(line=0),
        a_thread(line=-3),
        a_thread(line=2**40),
        a_thread(line=3.0),
        a_thread(line=True),
        a_thread(line="3"),
        a_thread(start_line=0),
        a_thread(side="middle"),
        a_thread(side="RIGHT"),
        a_thread(path="a\nb"),
        a_thread(path="a\u2028b"),
        a_thread(
            comments=[{**a_comment(), "author": {"login": "a\x85", "id": None, "is_bot": None}}]
        ),
        a_thread(path=5),
        a_thread(commit_sha="HEAD"),
        a_thread(outdated="false"),
    ],
)
def test_comments_with_a_bad_thread_is_a_mismatch(thread: dict[str, Any]) -> None:
    mismatch_of(lambda f: f.comments(PR), ok("comments", h.comments_doc(threads=[thread])))


def test_comments_with_a_duplicate_thread_id_is_a_mismatch() -> None:
    threads = [a_thread(comments=[a_comment("gh:rc:A")]), a_thread(comments=[a_comment("gh:rc:B")])]
    mismatch_of(lambda f: f.comments(PR), ok("comments", h.comments_doc(threads=threads)))


def test_comments_with_a_comment_id_shared_by_two_threads_is_a_mismatch() -> None:
    threads = [a_thread(THREAD_GH), a_thread("gh:rt:T2")]
    mismatch_of(lambda f: f.comments(PR), ok("comments", h.comments_doc(threads=threads)))


def test_comments_with_more_threads_than_the_cap_is_a_mismatch() -> None:
    comment = a_comment("gh:rc:C")
    threads = [a_thread(f"gh:rt:T{i}", [{**comment, "id": f"gh:rc:C{i}"}]) for i in range(100_001)]
    mismatch_of(lambda f: f.comments(PR), ok("comments", h.comments_doc(threads=threads)))


@pytest.mark.parametrize("path", ["pr", "head_sha", "threads", "complete"])
def test_comments_missing_a_field_is_a_mismatch(path: str) -> None:
    mismatch_of(lambda f: f.comments(PR), ok("comments", without(h.comments_doc(), path)))


def test_comments_for_another_pr_is_a_mismatch() -> None:
    other = h.comments_doc("github.com/acme/widgets#8")
    mismatch_of(lambda f: f.comments(PR), ok("comments", other))


def test_comments_with_a_lone_surrogate_body_is_a_mismatch() -> None:
    doc = h.comments_doc(threads=[a_thread(comments=[a_comment(body="x\ud83dy")])])
    mismatch_of(
        lambda f: f.comments(PR),
        ForgectlResult(
            command="comments",
            write=False,
            exit_code=0,
            stdout=json.dumps(doc, ensure_ascii=True).encode(),
        ),
    )


# --- whoami --------------------------------------------------------------------------------------


def test_whoami_reads_one_host() -> None:
    forge, gateway = forge_of(ok("whoami", h.whoami_doc(["github.com"])), pinned=True)
    me = forge.whoami("github.com")
    assert (me.host, me.backend, me.login, me.id, me.partial) == (
        "github.com",
        "github",
        "octo",
        "1",
        False,
    )
    assert gateway.calls[0].args == ("--host=github.com",)


def test_whoami_with_a_null_id_is_not_an_error() -> None:
    doc = h.whoami_doc(["github.com"])
    doc["hosts"][0]["id"] = None
    doc["hosts"][0]["login"] = None
    me = forge_of(ok("whoami", doc))[0].whoami("github.com")
    assert me.id is None and me.login is None


def partial_result(doc: dict[str, Any]) -> ForgectlResult:
    return ForgectlResult(
        command="whoami", write=False, exit_code=12, stdout=json.dumps(doc).encode()
    )


def test_whoami_exit_12_partial_results_returns_the_host_with_its_errors() -> None:
    doc = h.whoami_doc(
        ["github.com"],
        errors=[{"host": "github.com", "code": "auth_required", "message": "no token"}],
    )
    doc["hosts"][0]["id"] = None
    me = forge_of(partial_result(doc))[0].whoami("github.com")
    assert me.partial is True and me.id is None
    assert [(e.host, e.code) for e in me.errors] == [("github.com", "auth_required")]


def test_whoami_partial_through_the_pinned_fake() -> None:
    doc = h.whoami_doc(
        ["github.com"],
        errors=[{"host": "github.com", "code": "auth_required", "message": "no token"}],
    )
    doc["hosts"][0].update(id=None, login=None, me=None, me_matches_login=None, token_source="none")
    forge, _ = forge_of(partial_result(doc), pinned=True)
    assert forge.whoami("github.com").partial is True


@pytest.mark.parametrize(
    "doc",
    [
        h.whoami_doc(["gitlab.com"]),  # another host than asked
        h.whoami_doc(["github.com", "gitlab.com"]),  # more than asked
        {"hosts": [], "errors": []},
        {"hosts": "github.com", "errors": []},
        {"hosts": [], "errors": 5},
        {"errors": []},
        {"hosts": h.whoami_doc(["github.com"])["hosts"]},
        {
            **h.whoami_doc(["github.com"]),
            "errors": [{"host": "evil.test", "code": "x", "message": "m"}],
        },
        {**h.whoami_doc(["github.com"]), "errors": [{"host": "github.com", "code": "x"}]},
        {
            **h.whoami_doc(["github.com"]),
            "errors": [{"host": "github.com", "code": "x", "message": "m"}],
        },
        with_(h.whoami_doc(["github.com"]), "hosts.0.host", "GITHUB.com"),
        with_(h.whoami_doc(["github.com"]), "hosts.0.backend", "svn"),
        with_(h.whoami_doc(["github.com"]), "hosts.0.id", ""),
        with_(h.whoami_doc(["github.com"]), "hosts.0.id", 5),
        with_(h.whoami_doc(["github.com"]), "hosts.0.id", "a b"),
        with_(h.whoami_doc(["github.com"]), "hosts.0.login", ""),
        with_(h.whoami_doc(["github.com"]), "hosts.0.login", ["x"]),
    ],
)
def test_whoami_with_an_answer_that_does_not_fit_is_a_mismatch(doc: dict[str, Any]) -> None:
    mismatch_of(lambda f: f.whoami("github.com"), ok("whoami", doc))


def test_whoami_exit_12_with_no_errors_is_a_mismatch() -> None:
    mismatch_of(lambda f: f.whoami("github.com"), partial_result(h.whoami_doc(["github.com"])))


def test_whoami_exit_12_with_an_error_envelope_is_a_mismatch_not_a_passthrough() -> None:
    envelope = error_envelope("auth_required")
    mismatch_of(lambda f: f.whoami("github.com"), failing("whoami", 12, envelope))


def test_whoami_rejects_a_host_argument_before_any_call() -> None:
    forge, gateway = forge_of()
    for bad in (
        "",
        "-x",
        "a b",
        "UPPER.test",
        "x/y",
        "h\n",
        "a" * 400,
        "github.com:443",  # forgectl drops :443
        "ghe.test:0443",
        "ghe.test:65536",
        "ghe.test:0",
        "www.github.com",  # forgectl strips www. on built-in hosts
        "www.gitlab.com",
    ):
        with pytest.raises(GrammarError):
            forge.whoami(bad)
    with pytest.raises(GrammarError):
        forge.whoami(5)  # type: ignore[arg-type]
    assert gateway.calls == []


def test_whoami_keeps_hosts_forgectl_leaves_alone() -> None:
    for host in ("www.ghe.test", "ghe.test:8443", "ghe.test:65535"):
        forge, gateway = forge_of(ok("whoami", h.whoami_doc([host], backends={host: "github"})))
        assert forge.whoami(host).host == host
        assert gateway.calls[0].args == (f"--host={host}",)


# --- failures: passed through or lowered, per command ---------------------------------------------

READS: dict[str, Callable[[Forge], object]] = {
    "show": lambda f: f.show(PR),
    "diff": lambda f: f.diff(PR),
    "compare": lambda f: f.compare(PR, HEAD, OTHER),
    "files": lambda f: f.files(PR),
    "comments": lambda f: f.comments(PR),
    "whoami": lambda f: f.whoami("github.com"),
}


@pytest.mark.parametrize(
    ("code", "exit_code"),
    [
        ("not_found", 10),
        ("auth_required", 12),
        ("permission_denied", 12),
        ("rate_limited", 12),
        ("upstream_error", 12),
        ("network_error", 12),
        ("wrong_host_response", 12),
        ("unsupported_host_version", 12),
        ("internal", 1),
    ],
)
@pytest.mark.parametrize("command", ["show", "diff", "compare", "files", "comments"])
def test_a_read_passes_forgectls_failure_through_with_the_envelope(
    command: str, code: str, exit_code: int
) -> None:
    envelope = error_envelope(code, "host said no", details={"x": 1}, retry_after=3)
    outcome = failure_of(READS[command], failing(command, exit_code, envelope))
    error = outcome.document["error"]
    assert outcome.exit_code == exit_code and error["code"] == code
    assert error["lower_error"] == envelope["error"] and error["retry_after"] == 3
    if code == "not_found":
        assert error["details"] == {"x": 1, "what": "pr", "id": PR}
    else:
        assert error["details"] == {"x": 1}
    if exit_code == 12:
        assert error["landed"] is False and error["retry"] == "same_command"


@pytest.mark.parametrize("command", ["show", "diff", "compare", "files", "comments"])
def test_forgectls_own_tool_missing_passes_through_on_a_host_read(command: str) -> None:
    envelope = error_envelope(
        "tool_missing", "gh not found", details={"install": "brew install gh"}
    )
    outcome = failure_of(READS[command], failing(command, 12, envelope))
    error = outcome.document["error"]
    assert outcome.exit_code == 12 and error["code"] == "tool_missing"
    assert error["details"] == {"install": "brew install gh"}
    assert error["lower_error"] == envelope["error"]
    assert error["landed"] is False and error["retry"] == "same_command"


def test_forgectls_tool_missing_on_whoami_is_a_mismatch() -> None:
    envelope = error_envelope("tool_missing", details={"install": "brew install gh"})
    error = mismatch_of(READS["whoami"], failing("whoami", 12, envelope))
    assert error["lower_error"]["code"] == "tool_missing"


def test_whoami_passes_internal_through() -> None:
    outcome = failure_of(READS["whoami"], failing("whoami", 1, error_envelope("internal")))
    assert outcome.exit_code == 1 and outcome.document["error"]["code"] == "internal"


@pytest.mark.parametrize("command", ["show", "diff", "compare", "files", "comments"])
def test_exit_two_passes_only_invalid_pr_and_config_invalid_on_a_read(command: str) -> None:
    for code in ("invalid_pr", "config_invalid"):
        outcome = failure_of(READS[command], failing(command, 2, error_envelope(code)))
        assert outcome.exit_code == 2 and outcome.document["error"]["code"] == code
    for code in ("usage", "body_too_large"):
        outcome = failure_of(READS[command], failing(command, 2, error_envelope(code)))
        assert outcome.exit_code == 12
        assert outcome.document["error"]["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize("command", ["show", "diff", "compare", "files", "comments"])
def test_a_write_code_on_a_read_is_a_mismatch(command: str) -> None:
    cases = [
        (11, error_envelope("vetoed", hook="pre-post", hook_stderr="")),
        (12, error_envelope("outcome_unknown")),
        (
            12,
            error_envelope(
                "outcome_partial",
                details={"landed": {"ids": [], "thread_ids": []}, "failed": "note"},
            ),
        ),
        (12, error_envelope("quick_action_ran", details={"ids": []})),
        (12, error_envelope("invalid_position", details={"invalid": [{"index": 0}]})),
        (12, error_envelope("audit_failed", details={"outcome": "posted"})),
        (13, error_envelope("confirmation_required")),
        (20, error_envelope("refused", reason="stale_head")),
        (20, error_envelope("refused", reason="not_open", details={"state": "closed"})),
    ]
    for exit_code, envelope in cases:
        outcome = failure_of(READS[command], failing(command, exit_code, envelope))
        error = outcome.document["error"]
        assert outcome.exit_code == 12 and error["code"] == "lower_contract_mismatch"
        assert error["lower_error"]["code"] == envelope["error"]["code"]
        assert error["landed"] is False and error["retry"] == "same_command"


@pytest.mark.parametrize(
    ("command", "reason", "passes"),
    [
        ("show", "unknown_host", True),
        ("show", "too_large", False),
        ("files", "too_large", False),
        ("comments", "too_large", False),
        ("diff", "too_large", True),
        ("compare", "too_large", True),
        ("diff", "unknown_host", True),
    ],
)
def test_refusal_reasons_per_read(command: str, reason: str, passes: bool) -> None:
    envelope = error_envelope("refused", reason=reason)
    outcome = failure_of(READS[command], failing(command, 20, envelope))
    error = outcome.document["error"]
    if passes:
        assert outcome.exit_code == 20 and error["reason"] == reason
        assert error["lower_error"] == envelope["error"]
    else:
        assert error["code"] == "lower_contract_mismatch"


@pytest.mark.parametrize("command", ["show", "diff", "compare", "files", "comments"])
def test_a_dry_run_style_would_refuse_document_on_a_read_is_a_mismatch(command: str) -> None:
    doc = {"would_refuse": {"reason": "too_large", "message": "m", "details": None}}
    outcome = failure_of(READS[command], failing(command, 20, doc))
    error = outcome.document["error"]
    assert outcome.exit_code == 12 and error["code"] == "lower_contract_mismatch"
    assert error["lower_error"] == doc


@pytest.mark.parametrize("command", ["show", "diff", "compare", "files", "comments", "whoami"])
def test_exit_zero_with_nothing_or_a_signal_or_a_deadline_is_handled(command: str) -> None:
    mismatch_of(READS[command], raw(command, b""))
    timed = ForgectlResult(command=command, write=False, exit_code=None, timed_out=True)
    outcome = failure_of(READS[command], timed)
    assert outcome.document["error"]["code"] == "network_error"
    killed = ForgectlResult(command=command, write=False, exit_code=None, signal=9)
    mismatch_of(READS[command], killed)


@pytest.mark.parametrize("command", ["show", "diff", "files", "comments"])
def test_output_cut_at_the_limit_on_exit_zero_is_a_mismatch(command: str) -> None:
    cut = ForgectlResult(
        command=command, write=False, exit_code=0, stdout=b'{"pr": "', stdout_truncated=True
    )
    mismatch_of(READS[command], cut)


def test_unknown_exit_statuses_are_mismatches() -> None:
    for exit_code in (3, 4, 14, 21, 99, 127, 255):
        mismatch_of(READS["show"], failing("show", exit_code, error_envelope("not_found")))


def test_an_envelope_that_is_not_an_object_or_lacks_a_message_is_a_mismatch() -> None:
    for stdout in (b"[]", b"null", b'{"error": 5}', b'{"error": {"code": "not_found"}}', b"{}"):
        mismatch_of(READS["show"], raw("show", stdout, 10))


def test_the_whole_envelope_is_kept_and_its_message_is_cut() -> None:
    long = "m" * 10_000
    outcome = failure_of(READS["show"], failing("show", 10, error_envelope("not_found", long)))
    error = outcome.document["error"]
    assert len(error["message"].encode()) <= 4096 and error["lower_error"]["message"] == long


def test_a_missing_forgectl_surfaces_as_tool_missing_for_the_caller_to_lower() -> None:
    from pr_comment.gateway import ToolMissing

    forge = Forge(FakeGateway([ToolMissing("forgectl not found")]))
    with pytest.raises(ToolMissing):
        forge.show(PR)


def test_an_interrupted_call_propagates_untouched() -> None:
    import signal

    from pr_comment.gateway import ForgectlInterrupted

    interrupted = ForgectlInterrupted(signal.SIGTERM, ForgectlResult("show", False, None))
    forge = Forge(FakeGateway([interrupted]))
    with pytest.raises(ForgectlInterrupted):
        forge.show(PR)


def test_an_interrupted_version_read_during_a_mismatch_propagates() -> None:
    import signal

    from pr_comment.gateway import ForgectlInterrupted

    class Gateway(FakeGateway):
        def lower_version(self) -> str | None:
            raise ForgectlInterrupted(signal.SIGINT, ForgectlResult("--version", False, None))

    forge = Forge(Gateway([raw("show", b"nope")]))
    with pytest.raises(ForgectlInterrupted):
        forge.show(PR)


def test_lower_version_in_the_mismatch_is_sanitized_and_cut() -> None:
    gateway = FakeGateway([raw("show", b"{}")], version="forgectl \x1b[2J" + "9" * 100)
    with pytest.raises(LowerFailure) as caught:
        Forge(gateway).show(PR)
    version = caught.value.outcome.document["error"]["details"]["lower_version"]  # type: ignore[index]
    assert len(version) <= 64 and "\x1b" not in version


def test_lower_version_is_null_when_unknown() -> None:
    gateway = FakeGateway([raw("show", b"{}")], version=None)
    with pytest.raises(LowerFailure) as caught:
        Forge(gateway).show(PR)
    assert caught.value.outcome.document["error"]["details"]["lower_version"] is None  # type: ignore[index]


def test_the_mismatch_message_never_repeats_a_value_from_forgectl() -> None:
    secret = "SECRET-TOKEN-VALUE"
    doc = with_(h.show_doc(), "head.sha", secret)
    error = mismatch_of(lambda f: f.show(PR), ok("show", doc))
    assert secret not in json.dumps(error)


# --- through the real gateway and the fake forgectl process ---------------------------------------


def process_forge(forgectl: FakeForgectl, tracker: Tracker) -> Forge:
    gateway = ProcessGateway(environ=forgectl.environ(), deadline=60.0)
    return Forge(gateway)


def test_every_read_against_the_fake_process(forgectl: FakeForgectl, tracker: Tracker) -> None:
    thread = a_thread()
    forgectl.script(
        h.ok("show", h.show_doc()),
        h.ok("diff", h.diff_doc(diff=DIFF)),
        h.ok("compare", h.compare_doc(diff=DIFF)),
        h.ok("files", h.files_doc()),
        h.ok("comments", h.comments_doc(threads=[thread])),
        h.ok("whoami", h.whoami_doc(["github.com"])),
    )
    forge = process_forge(forgectl, tracker)
    shown = forge.show(PR)
    assert forge.diff(shown.pr, expect_head=shown.head_sha).diff == DIFF
    assert forge.compare(shown.pr, HEAD, OTHER).status == "ahead"
    assert forge.files(shown.pr).files[0].path == "a.py"
    assert forge.comments(shown.pr).threads[0].id == THREAD_GH
    assert forge.whoami(shown.host).login == "octo"
    calls = forgectl.calls()
    assert [c.subcommand for c in calls] == [
        "show",
        "diff",
        "compare",
        "files",
        "comments",
        "whoami",
    ]
    h.assert_edge_rules(calls, confirmed=False)


def test_a_scripted_forgectl_error_per_read_through_the_process(
    forgectl: FakeForgectl, tracker: Tracker
) -> None:
    forgectl.script(
        h.fail("show", "not_found", "no such PR"),
        h.fail("files", "rate_limited", "slow down", retry_after=7),
        h.refuse("diff", "too_large", "too big"),
    )
    forge = process_forge(forgectl, tracker)
    for call, code, exit_code in [
        (lambda: forge.show(PR), "not_found", 10),
        (lambda: forge.files(PR), "rate_limited", 12),
        (lambda: forge.diff(PR), "refused", 20),
    ]:
        with pytest.raises(LowerFailure) as caught:
            call()
        outcome = check_outcome(caught.value.outcome)
        assert outcome.exit_code == exit_code and outcome.document["error"]["code"] == code  # type: ignore[index]
    assert caught.value.outcome.document["error"]["reason"] == "too_large"  # type: ignore[index]


def test_whoami_partial_through_the_process(forgectl: FakeForgectl, tracker: Tracker) -> None:
    doc = h.whoami_doc(
        ["github.com"],
        errors=[{"host": "github.com", "code": "auth_required", "message": "no token"}],
    )
    doc["hosts"][0].update(id=None, login=None, me=None, me_matches_login=None, token_source="none")
    forgectl.script(h.partial("whoami", doc))
    me = process_forge(forgectl, tracker).whoami("github.com")
    assert me.partial and me.id is None


def test_a_bad_document_from_a_real_process_is_a_mismatch_with_the_real_version(
    forgectl: FakeForgectl, tracker: Tracker, tmp_path: Path
) -> None:
    # a program that prints a document the pinned fake would refuse to print: the real gateway
    # cannot tell, pr-comment must
    bad = tmp_path / "bin"
    bad.mkdir()
    script = bad / "forgectl"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "forgectl 9.9.9"; exit 0; fi\n'
        'echo \'{"pr": "github.com/acme/widgets#7"}\'\n'
    )
    script.chmod(0o755)
    gateway = ProcessGateway(environ={"PATH": str(bad), "HOME": str(tmp_path)}, deadline=30.0)
    with pytest.raises(LowerFailure) as caught:
        Forge(gateway).show(PR)
    error = caught.value.outcome.document["error"]  # type: ignore[index]
    assert error["code"] == "lower_contract_mismatch"
    assert error["details"] == {"command": "show", "lower_version": "forgectl 9.9.9"}
    forgectl.allow_unused_steps = True


# --- the draft's pr (docs/interface.md: Draft fields) -------------------------------------------

OLD_NAME = "github.com/acme/old-name#7"


def draft_match(draft_pr: str, *replies: Any) -> tuple[Forge, FakeGateway, Any]:
    from pr_comment.forge import match_draft_pr

    forge, gateway = forge_of(*replies)
    shown = Forge(FakeGateway([ok("show", h.show_doc())])).show(PR)
    return forge, gateway, lambda: match_draft_pr(forge, draft_pr, shown)


def test_the_same_pr_matches_without_a_call_and_case_is_ignored_for_ascii() -> None:
    for draft_pr in (PR, "GitHub.com/Acme/Widgets#7", "github.com/ACME/widgets#7"):
        _, gateway, run = draft_match(draft_pr)
        run()
        assert gateway.calls == []


def test_a_renamed_repo_with_the_same_pr_id_is_accepted_after_one_show() -> None:
    renamed = h.show_doc(PR)  # forgectl answers with the canonical name
    _, gateway, run = draft_match(OLD_NAME, ok("show", renamed))
    run()
    assert [(c.subcommand, c.args) for c in gateway.calls] == [("show", (OLD_NAME,))]


def test_a_different_pr_id_is_draft_pr_mismatch_with_both_values() -> None:
    other = h.show_doc("github.com/acme/other#9", pr_id="PR_kwDOOTHER")
    _, _, run = draft_match("github.com/acme/other#9", ok("show", other))
    with pytest.raises(LowerFailure) as caught:
        run()
    outcome = check_outcome(caught.value.outcome)
    assert outcome.exit_code == 2
    error = outcome.document["error"]  # type: ignore[index]
    assert error["code"] == "draft_pr_mismatch"
    assert error["details"] == {"draft_pr": "github.com/acme/other#9", "pr": PR}


def test_the_same_pr_id_on_another_backend_is_a_mismatch() -> None:
    gitlab = h.show_doc(h.GITLAB_PR, backend="gitlab", pr_id=h.show_doc()["pr_id"])
    _, _, run = draft_match(h.GITLAB_PR, ok("show", gitlab))
    with pytest.raises(LowerFailure) as caught:
        run()
    assert caught.value.outcome.document["error"]["code"] == "draft_pr_mismatch"  # type: ignore[index]


def test_the_same_pr_id_on_another_host_or_number_is_a_mismatch() -> None:
    for pr in ("ghe.example.com/acme/old-name#7", "github.com/acme/old-name#8"):
        _, _, run = draft_match(pr, ok("show", h.show_doc(pr)))
        with pytest.raises(LowerFailure) as caught:
            run()
        assert caught.value.outcome.document["error"]["code"] == "draft_pr_mismatch"  # type: ignore[index]


@pytest.mark.parametrize(
    "reply",
    [
        failing("show", 10, error_envelope("not_found")),
        failing("show", 2, error_envelope("invalid_pr")),
        failing("show", 20, error_envelope("refused", reason="unknown_host")),
    ],
)
def test_a_draft_pr_forgectl_cannot_find_is_a_mismatch(reply: ForgectlResult) -> None:
    _, _, run = draft_match("github.com/acme/gone#3", reply)
    with pytest.raises(LowerFailure) as caught:
        run()
    outcome = caught.value.outcome
    assert outcome.exit_code == 2 and outcome.document["error"]["code"] == "draft_pr_mismatch"  # type: ignore[index]


@pytest.mark.parametrize(
    "reply",
    [
        failing("show", 12, error_envelope("auth_required")),
        failing("show", 12, error_envelope("rate_limited")),
        raw("show", b"{}"),
    ],
)
def test_other_failures_of_that_show_are_not_a_mismatch(reply: ForgectlResult) -> None:
    _, _, run = draft_match("github.com/acme/gone#3", reply)
    with pytest.raises(LowerFailure) as caught:
        run()
    assert caught.value.outcome.document["error"]["code"] != "draft_pr_mismatch"  # type: ignore[index]


@pytest.mark.parametrize("draft_pr", ["-x", "", "a b#1", "plain", "x#1\n", "café/x#1", "\ud800#1"])
def test_a_draft_pr_that_is_not_a_pr_reference_is_a_mismatch_without_a_call(draft_pr: str) -> None:
    _, gateway, run = draft_match(draft_pr)
    with pytest.raises(LowerFailure) as caught:
        run()
    error = caught.value.outcome.document["error"]  # type: ignore[index]
    assert error["code"] == "draft_pr_mismatch" and gateway.calls == []
    check_outcome(caught.value.outcome)


def test_a_huge_draft_pr_is_cut_in_the_details() -> None:
    _, _, run = draft_match("x" * 100_000)
    with pytest.raises(LowerFailure) as caught:
        run()
    details = caught.value.outcome.document["error"]["details"]  # type: ignore[index]
    assert len(details["draft_pr"].encode()) <= 4096


# --- goldens and the config seam -----------------------------------------------------------------


def unpinned(document: Any) -> Any:
    """The document with the pinned release replaced by a placeholder, so a golden holds no copy
    of the number in ``forgectl_pin.json``."""
    text = json.dumps(document).replace(json.dumps(PINNED_LOWER_VERSION)[1:-1], "forgectl <pinned>")
    return json.loads(text)


def test_golden_outcomes(golden_json: Callable[[str, Any], None]) -> None:
    missing_head = failure_of(lambda f: f.show(PR), ok("show", without(h.show_doc(), "head.sha")))
    golden_json("forge/show-missing-head-sha.json", unpinned(missing_head.document))
    cases = {
        "not-found": failing("show", 10, error_envelope("not_found", "no such PR")),
        "rate-limited": failing(
            "show", 12, error_envelope("rate_limited", "slow down", retry_after=7)
        ),
        "usage-is-a-mismatch": failing("show", 2, error_envelope("usage", "bad flag")),
        "write-code-on-a-read": failing(
            "show", 11, error_envelope("vetoed", "no", hook="pre-post", hook_stderr="")
        ),
    }
    for name, reply in cases.items():
        document = failure_of(lambda f: f.show(PR), reply).document
        golden_json(f"forge/show-{name}.json", unpinned(document))
    too_large = failing("diff", 20, error_envelope("refused", "too big", reason="too_large"))
    golden_json("forge/diff-too-large.json", failure_of(lambda f: f.diff(PR), too_large).document)
    stale = failure_of(lambda f: f.diff(PR, expect_head=OTHER), ok("diff", h.diff_doc()))
    golden_json("forge/diff-stale-head.json", stale.document)
    _, _, run = draft_match(
        "github.com/acme/other#9",
        ok("show", h.show_doc("github.com/acme/other#9", pr_id="PR_other")),
    )
    with pytest.raises(LowerFailure) as caught:
        run()
    golden_json("forge/draft-pr-mismatch.json", caught.value.outcome.document)


def test_the_canonical_repo_from_show_selects_the_per_repo_config(tmp_path: Path) -> None:
    from pr_comment.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text("repos:\n  github.com/ACME/Widgets:\n    allow_auto_approve: true\n")
    path.chmod(0o600)
    loaded = load_config(str(path), env={})
    shown = Forge(FakeGateway([ok("show", h.show_doc())])).show(PR)
    assert loaded.for_repo(shown.repo).allow_auto_approve is True
    assert loaded.for_repo("github.com/acme/other").allow_auto_approve is False
