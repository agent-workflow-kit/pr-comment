"""``pr-comment draft new``: a skeleton draft read through forgectl (docs/interface.md: draft new).

The forgectl documents come from the pinned fake (``PinnedGateway``, the harness builders), the
source draft of ``--from`` is untrusted input. The process tests run the real gateway against the
fake forgectl binary and check that nothing but reads reaches it.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pr_comment import draft as draft_module
from pr_comment.cli import main
from pr_comment.draft import new_draft_id, run_new, ulid
from pr_comment.forge import Forge
from pr_comment.gateway import ForgectlResult
from pr_comment.output import Outcome, check_outcome
from tests import forgectl_harness as h
from tests.fake_gateway import FakeGateway, PinnedGateway, ok
from tests.forgectl_harness import FakeForgectl
from tests.schema_harness import validate
from tests.test_draft import CANARY, good

PR = h.PR
HEAD, OTHER, BASE = h.HEAD, h.OTHER_HEAD, h.BASE_SHA
ID_RE = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
NOW_MS = 1_700_000_000_123
ENTROPY = bytes(range(1, 11))

Doc = dict[str, Any]


def failing(subcommand: str, exit_code: int, code: str, **extra: Any) -> ForgectlResult:
    envelope = {"error": {"code": code, "message": "m", **extra}}
    return ForgectlResult(
        command=subcommand, write=False, exit_code=exit_code, stdout=json.dumps(envelope).encode()
    )


def too_large() -> ForgectlResult:
    return failing("diff", 20, "refused", reason="too_large")


def diff_of(**kw: Any) -> ForgectlResult:
    return ok("diff", h.diff_doc(**kw))


def show_of(pr: str = PR, **kw: Any) -> ForgectlResult:
    return ok("show", h.show_doc(pr, **kw))


def forge_of(*replies: Any, pinned: bool = True) -> tuple[Forge, FakeGateway]:
    """``pinned=False`` feeds documents forgectl never prints (a hostile or broken forgectl)."""
    gateway: FakeGateway = PinnedGateway(list(replies)) if pinned else FakeGateway(list(replies))
    return Forge(gateway), gateway


def new(
    *replies: Any,
    event: str | None = None,
    from_draft: str | None = None,
    stdin: Any = None,
    pr: str = PR,
    pinned: bool = True,
) -> tuple[Outcome, FakeGateway]:
    forge, gateway = forge_of(*replies, pinned=pinned)
    outcome = run_new(
        forge,
        pr,
        event=event,
        from_draft=from_draft,
        stdin=stdin,
        now_ms=NOW_MS,
        entropy=ENTROPY,
    )
    return check_outcome(outcome), gateway


def document_of(outcome: Outcome) -> Doc:
    assert outcome.exit_code == 0, outcome.document
    assert outcome.document is not None
    return outcome.document


def error_of(outcome: Outcome) -> Doc:
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    return outcome.document["error"]  # type: ignore[no-any-return]


def assert_checks_clean(document: Doc) -> None:
    """The document validates against both schemas and passes the ``draft check`` rules."""
    validate("pr-comment/cli/draft-new.json", document)
    validate("pr-comment/review-draft.json", document)
    result = draft_module.check_result(json.dumps(document).encode())
    assert result["valid"] is True, result["errors"]


def source_file(tmp_path: Path, doc: Doc | None = None, name: str = "source.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(good() if doc is None else doc), encoding="utf-8")
    return str(path)


def source_at(head: str = HEAD, pr: str = PR) -> Doc:
    doc = good()
    doc["pr"] = pr
    doc["anchor"]["head_sha"] = head
    return doc


# --- the ULID --------------------------------------------------------------------------------


def test_ulid_known_vectors() -> None:
    assert ulid(0, bytes(10)) == "0" * 26
    assert ulid(2**48 - 1, b"\xff" * 10) == "7" + "Z" * 25
    assert ulid(1, bytes(10)) == "0" * 9 + "1" + "0" * 16  # the time is the first 10 characters
    assert ulid(0, bytes(9) + b"\x01") == "0" * 25 + "1"  # the entropy is the last 16


def test_ulid_time_prefix_decodes_to_the_millisecond() -> None:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    value = ulid(NOW_MS, ENTROPY)
    assert ID_RE.fullmatch(value)
    assert int("".join(f"{alphabet.index(c):05b}" for c in value[:10]), 2) == NOW_MS


@pytest.mark.parametrize(
    ("now_ms", "entropy"),
    [(-1, bytes(10)), (2**48, bytes(10)), (0, bytes(9)), (0, bytes(11)), (True, bytes(10))],
)
def test_ulid_refuses_a_value_outside_its_ranges(now_ms: int, entropy: bytes) -> None:
    with pytest.raises((ValueError, TypeError)):
        ulid(now_ms, entropy)


def test_fresh_draft_ids_match_the_grammar_and_do_not_repeat() -> None:
    ids = {new_draft_id() for _ in range(2000)}
    assert len(ids) == 2000
    assert all(ID_RE.fullmatch(i) for i in ids)


def test_ulids_sort_by_time() -> None:
    assert ulid(NOW_MS, ENTROPY) < ulid(NOW_MS + 1, bytes(10))


# --- the skeleton ----------------------------------------------------------------------------


def test_skeleton_from_diff_then_show(golden_json: Callable[[str, Any], None]) -> None:
    outcome, gateway = new(diff_of(), show_of())
    document = document_of(outcome)
    assert [c.subcommand for c in gateway.calls] == ["diff", "show"]
    assert [c.args for c in gateway.calls] == [(PR,), (PR,)]
    assert document == {
        "schema_version": 1,
        "draft_id": ulid(NOW_MS, ENTROPY),
        "pr": PR,
        "anchor": {"head_sha": HEAD, "base_ref": "main", "base_sha": BASE, "merge_base_sha": BASE},
        "event": "comment",
        "body": "",
        "findings": [],
    }
    assert_checks_clean(document)
    golden_json("draft/new-skeleton.json", document)


def test_the_canonical_pr_of_show_is_used_for_a_url_argument() -> None:
    url = "https://github.com/acme/widgets/pull/7"
    outcome, gateway = new(diff_of(), show_of(), pr=url)
    assert document_of(outcome)["pr"] == PR
    assert [c.args for c in gateway.calls] == [(url,), (url,)]


def test_a_renamed_repo_is_written_as_forgectl_names_it() -> None:
    renamed = "github.com/acme/renamed#7"
    url = "https://github.com/acme/widgets/pull/7"  # the old name: the host redirects
    outcome, _ = new(diff_of(pr=renamed), show_of(renamed), pr=url, pinned=False)
    assert document_of(outcome)["pr"] == renamed


def test_a_gitlab_skeleton_passes_the_checks() -> None:
    outcome, _ = new(
        diff_of(pr=h.GITLAB_PR), show_of(h.GITLAB_PR, backend="gitlab"), pr=h.GITLAB_PR
    )
    assert_checks_clean(document_of(outcome))


def test_base_sha_comes_from_show_not_from_diff() -> None:
    shown = h.show_doc()
    shown["base"]["sha"] = None
    outcome, _ = new(diff_of(), ok("show", shown))
    anchor = document_of(outcome)["anchor"]
    assert anchor["base_sha"] is None and anchor["merge_base_sha"] == BASE
    assert_checks_clean(document_of(outcome))


def test_merge_base_null_from_diff_is_kept() -> None:
    document = h.diff_doc()
    document["merge_base_sha"] = None
    outcome, _ = new(ok("diff", document), show_of())
    assert document_of(outcome)["anchor"]["merge_base_sha"] is None


@pytest.mark.parametrize("event", ["comment", "request_changes"])
def test_event_override(event: str) -> None:
    outcome, _ = new(diff_of(), show_of(), event=event)
    assert document_of(outcome)["event"] == event


def test_approve_is_never_a_draft_event() -> None:
    with pytest.raises(ValueError):
        new(diff_of(), show_of(), event="approve")


def test_a_second_run_gets_a_second_draft_id() -> None:
    forge, _ = forge_of(diff_of(), show_of(), diff_of(), show_of())
    first = run_new(forge, PR, event=None, from_draft=None, stdin=None)
    second = run_new(forge, PR, event=None, from_draft=None, stdin=None)
    assert document_of(first)["draft_id"] != document_of(second)["draft_id"]


# --- too large -------------------------------------------------------------------------------


def test_too_large_diff_gives_a_null_merge_base_and_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="pr_comment"):
        outcome, gateway = new(too_large(), show_of())
    document = document_of(outcome)
    assert document["anchor"] == {
        "head_sha": HEAD,
        "base_ref": "main",
        "base_sha": BASE,
        "merge_base_sha": None,
    }
    assert [c.subcommand for c in gateway.calls] == ["diff", "show"]
    assert_checks_clean(document)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "too large" in warnings[0]


def test_too_large_needs_no_second_read_and_never_a_stale_head() -> None:
    _, gateway = new(too_large(), show_of())
    assert len(gateway.calls) == 2


@pytest.mark.parametrize(
    "reply",
    [
        failing("diff", 10, "not_found"),
        failing("diff", 12, "upstream_error"),
    ],
)
def test_any_other_diff_failure_ends_the_run_before_show(reply: ForgectlResult) -> None:
    outcome, gateway = new(reply)
    assert outcome.exit_code != 0
    assert [c.subcommand for c in gateway.calls] == ["diff"]


def test_a_failing_show_ends_the_run() -> None:
    outcome, _ = new(diff_of(), failing("show", 10, "not_found"))
    assert outcome.exit_code == 10
    assert error_of(outcome)["details"]["what"] == "pr"


# --- the race between the two reads ----------------------------------------------------------


def test_heads_that_differ_are_read_once_more() -> None:
    outcome, gateway = new(diff_of(head_sha=OTHER), show_of(), diff_of(), show_of())
    assert document_of(outcome)["anchor"]["head_sha"] == HEAD
    assert [c.subcommand for c in gateway.calls] == ["diff", "show", "diff", "show"]


def test_heads_that_differ_twice_are_stale_head_with_the_head_of_show() -> None:
    outcome, gateway = new(
        diff_of(head_sha=OTHER),
        show_of(),
        diff_of(head_sha=OTHER),
        show_of(head={"sha": "d" * 40, "ref": "topic", "repo": "acme/widgets", "is_fork": False}),
    )
    assert outcome.exit_code == 20
    error = error_of(outcome)
    assert error["code"] == "refused" and error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": "d" * 40}
    assert len(gateway.calls) == 4  # one retry, no third


def test_a_head_that_moves_between_the_retry_reads_is_stale_head() -> None:
    outcome, _ = new(
        diff_of(head_sha=OTHER),
        show_of(),
        diff_of(),
        show_of(head={"sha": OTHER, "ref": "topic", "repo": "acme/widgets", "is_fork": False}),
    )
    assert error_of(outcome)["reason"] == "stale_head"


def test_a_too_large_retry_read_uses_the_head_of_show_alone() -> None:
    outcome, _ = new(diff_of(head_sha=OTHER), show_of(), too_large(), show_of())
    document = document_of(outcome)
    assert document["anchor"]["merge_base_sha"] is None and document["anchor"]["head_sha"] == HEAD


# --- hostile forgectl answers ----------------------------------------------------------------


def test_a_diff_of_another_pr_is_a_contract_mismatch() -> None:
    outcome, _ = new(diff_of(pr="github.com/acme/widgets#8"), show_of(), pinned=False)
    assert outcome.exit_code == 12 and error_of(outcome)["code"] == "lower_contract_mismatch"


def test_a_diff_of_another_pr_is_caught_for_a_url_argument_too() -> None:
    url = "https://github.com/acme/widgets/pull/7"
    outcome, _ = new(diff_of(pr="github.com/acme/widgets#9"), show_of(), pr=url, pinned=False)
    assert outcome.exit_code == 12 and error_of(outcome)["details"]["command"] == "diff"


def test_a_diff_of_another_host_is_caught_for_a_url_argument() -> None:
    url = "https://github.com/acme/widgets/pull/7"
    outcome, _ = new(diff_of(pr="gitlab.com/acme/widgets#7"), show_of(), pr=url, pinned=False)
    assert outcome.exit_code == 12


def test_a_show_of_another_pr_is_a_contract_mismatch() -> None:
    outcome, _ = new(diff_of(), show_of("github.com/acme/widgets#8"), pinned=False)
    assert outcome.exit_code == 12


def test_a_base_ref_a_draft_cannot_hold_is_refused_not_written() -> None:
    for ref in ("refs/heads/main", "-main", "refs", "a..b"):
        shown = h.show_doc()
        shown["base"]["ref"] = ref
        outcome, _ = new(diff_of(), ok("show", shown))
        assert outcome.exit_code in (12, 2), ref
        assert outcome.document is not None and "error" in outcome.document
        assert ref not in json.dumps(outcome.document)


def test_a_base_ref_with_unicode_and_a_long_name_is_written_when_valid() -> None:
    for ref in ("feature/ünï", "release/" + "x" * 900):
        shown = h.show_doc()
        shown["base"]["ref"] = ref
        outcome, _ = new(diff_of(), ok("show", shown))
        assert document_of(outcome)["anchor"]["base_ref"] == ref
        assert_checks_clean(document_of(outcome))


def test_a_base_ref_the_draft_cannot_hold_says_the_pr_cannot_be_written_into_a_draft() -> None:
    shown = h.show_doc()
    shown["base"]["ref"] = "refs/heads/main"
    outcome, _ = new(diff_of(), ok("show", shown))
    assert outcome.exit_code == 12
    assert "cannot be written into a draft" in error_of(outcome)["message"]
    assert "base branch" not in error_of(outcome)["message"]


# --- the clock -------------------------------------------------------------------------------


@pytest.mark.parametrize("nanoseconds", [-1_000_000, 3 * 10**20])
def test_a_clock_outside_the_ulid_range_is_a_plain_internal_error_before_any_read(
    monkeypatch: pytest.MonkeyPatch, nanoseconds: int
) -> None:
    monkeypatch.setattr(draft_module.time, "time_ns", lambda: nanoseconds)
    forge, gateway = forge_of()
    outcome = check_outcome(run_new(forge, PR, event=None, from_draft=None, stdin=None))
    assert outcome.exit_code == 1 and error_of(outcome)["code"] == "internal"
    assert error_of(outcome)["message"] == "the system clock is out of range"
    assert gateway.calls == []


# --- --from ----------------------------------------------------------------------------------


def test_from_copies_the_content_and_takes_a_fresh_anchor_and_id(tmp_path: Path) -> None:
    source = source_at()
    source["anchor"]["base_ref"] = "old-base"
    source["anchor"]["base_sha"] = "1" * 40
    source["anchor"]["merge_base_sha"] = "2" * 40
    outcome, _ = new(diff_of(), show_of(), from_draft=source_file(tmp_path, source))
    document = document_of(outcome)
    for key in ("event", "body", "findings", "unanchored", "meta"):
        assert document[key] == source[key]
    assert document["draft_id"] == ulid(NOW_MS, ENTROPY) != source["draft_id"]
    assert document["anchor"] == {
        "head_sha": HEAD,
        "base_ref": "main",
        "base_sha": BASE,
        "merge_base_sha": BASE,
    }
    assert_checks_clean(document)


def test_from_without_unanchored_or_meta_adds_neither(tmp_path: Path) -> None:
    source = source_at()
    del source["unanchored"], source["meta"]
    document = document_of(new(diff_of(), show_of(), from_draft=source_file(tmp_path, source))[0])
    assert "unanchored" not in document and "meta" not in document
    assert_checks_clean(document)


def test_from_keeps_left_side_findings_unchanged(tmp_path: Path) -> None:
    source = source_at()
    source["findings"][0]["side"] = "left"
    document = document_of(new(diff_of(), show_of(), from_draft=source_file(tmp_path, source))[0])
    assert document["findings"][0]["side"] == "left"


def test_from_event_override(tmp_path: Path) -> None:
    path = source_file(tmp_path, source_at())  # the source event is comment
    document = document_of(new(diff_of(), show_of(), from_draft=path, event="request_changes")[0])
    assert document["event"] == "request_changes"


def test_from_does_not_alias_the_source_between_runs(tmp_path: Path) -> None:
    path = source_file(tmp_path, source_at())
    first = document_of(new(diff_of(), show_of(), from_draft=path)[0])
    first["findings"].append("changed")
    second = document_of(new(diff_of(), show_of(), from_draft=path)[0])
    assert len(second["findings"]) == 3


def test_from_a_stale_anchor_is_stale_head(tmp_path: Path) -> None:
    outcome, _ = new(diff_of(), show_of(), from_draft=source_file(tmp_path, source_at(OTHER)))
    assert outcome.exit_code == 20
    error = error_of(outcome)
    assert error["reason"] == "stale_head" and error["details"] == {"head_sha": HEAD}


def test_from_checks_the_head_after_the_reads_settle(tmp_path: Path) -> None:
    path = source_file(tmp_path, source_at(OTHER))
    outcome, gateway = new(
        diff_of(head_sha=OTHER), show_of(), diff_of(), show_of(), from_draft=path
    )
    assert error_of(outcome)["reason"] == "stale_head"
    assert len(gateway.calls) == 4


def test_from_a_draft_of_another_pr_is_draft_pr_mismatch(tmp_path: Path) -> None:
    path = source_file(tmp_path, source_at(pr="github.com/acme/other#7"))
    forge, _ = forge_of(diff_of(), show_of(), failing("show", 10, "not_found"))
    outcome = run_new(forge, PR, event=None, from_draft=path, stdin=None)
    assert outcome.exit_code == 2
    error = error_of(outcome)
    assert error["code"] == "draft_pr_mismatch"
    assert error["details"] == {"draft_pr": "github.com/acme/other#7", "pr": PR}


def test_from_a_renamed_repo_with_the_same_pr_id_is_accepted(tmp_path: Path) -> None:
    path = source_file(tmp_path, source_at(pr="github.com/acme/old-name#7"))
    renamed_source = h.show_doc("github.com/acme/old-name#7")  # same pr_id as PR's
    outcome, _ = new(diff_of(), show_of(), ok("show", renamed_source), from_draft=path)
    assert document_of(outcome)["pr"] == PR


def test_from_is_read_before_any_forgectl_call(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    forge, gateway = forge_of()
    outcome = run_new(forge, PR, event=None, from_draft=str(broken), stdin=None)
    assert outcome.exit_code == 2 and error_of(outcome)["code"] == "invalid_draft"
    assert gateway.calls == []


def test_from_an_invalid_draft_never_echoes_it(tmp_path: Path) -> None:
    source = source_at()
    source["findings"][0]["line"] = 0
    source["body"] = CANARY
    forge, _ = forge_of()
    outcome = run_new(forge, PR, event=None, from_draft=source_file(tmp_path, source), stdin=None)
    assert outcome.exit_code == 2
    assert CANARY not in json.dumps(outcome.document)


def test_from_a_draft_with_a_control_character_is_invalid_text(tmp_path: Path) -> None:
    source = source_at()
    source["body"] = "a\x07b"
    forge, _ = forge_of()
    outcome = run_new(forge, PR, event=None, from_draft=source_file(tmp_path, source), stdin=None)
    assert error_of(outcome)["code"] == "invalid_text"


def test_from_an_unsupported_version(tmp_path: Path) -> None:
    source = source_at()
    source["schema_version"] = 2
    forge, _ = forge_of()
    outcome = run_new(forge, PR, event=None, from_draft=source_file(tmp_path, source), stdin=None)
    assert error_of(outcome)["code"] == "unsupported_draft_version"


def test_from_a_symlink_a_fifo_a_directory_and_a_missing_file(tmp_path: Path) -> None:
    real = source_file(tmp_path, source_at())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    fifo = tmp_path / "fifo.json"
    os.mkfifo(fifo)
    directory = tmp_path / "dir.json"
    directory.mkdir()
    for path in (str(link), str(fifo), str(directory), str(tmp_path / "missing.json"), ""):
        forge, gateway = forge_of()
        outcome = run_new(forge, PR, event=None, from_draft=path, stdin=None)
        assert outcome.exit_code == 2 and error_of(outcome)["code"] == "invalid_draft", path
        assert gateway.calls == []


def test_from_a_huge_path_is_refused_not_crashed(tmp_path: Path) -> None:
    for path in ("a" * 5000 + ".json", "x/" * 3000, "a\x00b"):
        forge, _ = forge_of()
        outcome = run_new(forge, PR, event=None, from_draft=path, stdin=None)
        assert outcome.exit_code == 2 and error_of(outcome)["code"] == "invalid_draft"


def test_from_stdin_is_read_when_the_source_is_a_dash() -> None:
    raw = json.dumps(source_at()).encode()
    document = document_of(new(diff_of(), show_of(), from_draft="-", stdin=io.BytesIO(raw))[0])
    assert document["body"] == "summary"


def test_from_stdin_over_the_cap_is_refused() -> None:
    forge, gateway = forge_of()
    big = io.BytesIO(b" " * (draft_module.MAX_DRAFT_BYTES + 1))
    outcome = run_new(forge, PR, event=None, from_draft="-", stdin=big)
    assert error_of(outcome)["code"] == "invalid_draft" and gateway.calls == []


def test_from_a_source_that_grows_past_the_cap_when_escaped_is_refused_and_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_at()
    source["body"] = "\u00e9" * 200
    raw = json.dumps(source, ensure_ascii=False).encode("utf-8")
    path = tmp_path / "wide.json"
    path.write_bytes(raw)
    monkeypatch.setattr(draft_module, "MAX_DRAFT_BYTES", len(raw) + 50)
    assert draft_module.check_result(raw)["valid"] is True  # the source itself is within the cap
    outcome, _ = new(diff_of(), show_of(), from_draft=str(path))
    assert outcome.exit_code == 2 and not outcome.print_document
    error = error_of(outcome)
    assert error["code"] == "invalid_draft"
    assert "draft is larger than 16 MiB" in json.dumps(error["details"])


def test_from_does_not_change_the_source_file(tmp_path: Path) -> None:
    path = source_file(tmp_path, source_at())
    before = Path(path).read_bytes()
    new(diff_of(), show_of(), from_draft=path)
    assert Path(path).read_bytes() == before
    assert sorted(
        p.name for p in tmp_path.iterdir() if p.name not in ("private-home", "empty-path")
    ) == ["source.json"]


def test_a_copied_source_is_a_deep_copy_of_a_nested_meta(tmp_path: Path) -> None:
    source = source_at()
    source["meta"] = {"deep": {"list": [1, 2, {"x": None}]}}
    document = document_of(new(diff_of(), show_of(), from_draft=source_file(tmp_path, source))[0])
    assert document["meta"] == copy.deepcopy(source["meta"])


# --- the command line ------------------------------------------------------------------------


@pytest.fixture
def process(forgectl: FakeForgectl, monkeypatch: pytest.MonkeyPatch) -> FakeForgectl:
    for name, value in forgectl.environ().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(forgectl.directory / "run"))
    return forgectl


def run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_cli_prints_the_draft_with_json(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    process.script(h.ok("diff", h.diff_doc()), h.ok("show", h.show_doc()))
    code, out, _ = run_cli(capsys, "draft", "new", PR, "--json")
    assert code == 0
    document = json.loads(out)
    assert_checks_clean(document)
    assert document["pr"] == PR and ID_RE.fullmatch(document["draft_id"])


def test_cli_prints_the_draft_without_json_too(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    process.script(h.ok("diff", h.diff_doc()), h.ok("show", h.show_doc()))
    code, out, _ = run_cli(capsys, "draft", "new", PR)
    assert code == 0
    assert_checks_clean(json.loads(out))


def test_cli_prints_nothing_on_stdout_for_an_error_without_json(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    process.script(h.fail("diff", "not_found", "no such pr"))
    code, out, err = run_cli(capsys, "draft", "new", PR)
    assert code == 10 and out == "" and err


def test_cli_the_printed_draft_passes_draft_check(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    process.script(h.ok("diff", h.diff_doc()), h.ok("show", h.show_doc()))
    _, out, _ = run_cli(capsys, "draft", "new", PR, "--event", "request_changes")
    path = tmp_path / "skeleton.json"
    path.write_text(out, encoding="utf-8")
    code, check_out, _ = run_cli(capsys, "draft", "check", str(path), "--json")
    assert code == 0 and json.loads(check_out)["valid"] is True


def test_cli_a_process_test_proves_only_reads_and_no_audit_record(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    process.script(h.ok("diff", h.diff_doc()), h.ok("show", h.show_doc()))
    code, _, _ = run_cli(capsys, "draft", "new", PR, "--json")
    assert code == 0
    calls = process.calls()
    assert [c.subcommand for c in calls] == ["diff", "show"]
    assert not any(c.is_write or c.is_dry_run for c in calls)
    assert all("--yes" not in c.argv and not c.stdin for c in calls)
    home = Path(os.environ["HOME"])
    assert not home.exists() or not any(home.rglob("*.jsonl"))  # no audit.jsonl, no log.jsonl
    assert [p.name for p in tmp_path.glob("**/*.jsonl")] == ["calls.jsonl"]  # the fake's own log


def test_cli_too_large_succeeds_with_a_null_merge_base(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    process.script(h.refuse("diff", "too_large", "too big"), h.ok("show", h.show_doc()))
    code, out, err = run_cli(capsys, "draft", "new", PR, "--json")
    assert code == 0
    document = json.loads(out)
    assert document["anchor"]["merge_base_sha"] is None
    assert_checks_clean(document)
    assert "too large" in err


def test_cli_quiet_drops_the_warning(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    process.script(h.refuse("diff", "too_large", "too big"), h.ok("show", h.show_doc()))
    code, _, err = run_cli(capsys, "draft", "new", PR, "--json", "--quiet")
    assert code == 0 and err == ""


def test_cli_from_a_stale_anchor_exits_20(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    process.script(h.ok("diff", h.diff_doc()), h.ok("show", h.show_doc()))
    path = source_file(tmp_path, source_at(OTHER))
    code, out, _ = run_cli(capsys, "draft", "new", PR, "--from", path, "--json")
    assert code == 20
    error = json.loads(out)["error"]
    assert error["reason"] == "stale_head" and error["details"] == {"head_sha": HEAD}
    validate("pr-comment/cli/error.json", json.loads(out))


def test_cli_stale_heads_twice_exit_20(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    process.script(
        h.ok("diff", h.diff_doc(head_sha=OTHER)),
        h.ok("show", h.show_doc()),
        h.ok("diff", h.diff_doc(head_sha=OTHER)),
        h.ok("show", h.show_doc()),
    )
    code, out, _ = run_cli(capsys, "draft", "new", PR, "--json")
    assert code == 20 and json.loads(out)["error"]["reason"] == "stale_head"
    assert len(process.calls()) == 4


def test_cli_from_an_invalid_draft_runs_no_forgectl(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    path = tmp_path / "bad.json"
    path.write_text("[]", encoding="utf-8")
    code, out, _ = run_cli(capsys, "draft", "new", PR, "--from", str(path), "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_draft"
    assert process.calls() == []


def test_cli_a_missing_forgectl_is_tool_missing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    code, out, _ = run_cli(capsys, "draft", "new", PR, "--json")
    assert code == 12 and json.loads(out)["error"]["code"] == "tool_missing"


def test_cli_a_broken_config_is_config_invalid_before_any_read(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("timeout: -5s\n", encoding="utf-8")
    code, out, _ = run_cli(capsys, "draft", "new", PR, "--config", str(config), "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "config_invalid"
    assert process.calls() == []


def test_cli_an_invalid_pr_is_refused_before_any_read(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_cli(capsys, "draft", "new", "not a pr", "--json")
    assert code == 2 and json.loads(out)["error"]["code"] == "invalid_pr"
    assert process.calls() == []


def test_cli_a_base_ref_a_draft_cannot_hold_is_exit_12_with_ascii_output(
    process: FakeForgectl, capsys: pytest.CaptureFixture[str]
) -> None:
    shown = h.show_doc()
    shown["base"]["ref"] = "refs/heads/\u00e9"
    process.script(h.ok("diff", h.diff_doc()), h.ok("show", shown))
    code, out, err = run_cli(capsys, "draft", "new", PR, "--json")
    assert code == 12 and out.isascii() and "refs/heads" not in out + err
