"""Tests for re-anchoring a draft onto a newer head (docs/interface.md, Re-anchoring).

A wrong re-anchoring posts a comment on the wrong code, so these tests check the cases where the
answer must be "move": left-side findings after a base change, removed lines, removed or renamed
files, an unreadable diff. Oracle tests run real ``git diff`` in scratch repositories and check
that every finding that maps lands on the same text.
"""

from __future__ import annotations

import copy
import json
import os
import random
import subprocess
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from pr_comment.diffmap import DiffMapper
from pr_comment.draft import Draft, validate_document
from pr_comment.reanchor import (
    BASE_CHANGED,
    CompareContractError,
    StaleHeadError,
    left_side_ok,
    mapper_from_compare,
    reanchor,
)
from tests.schema_harness import validate

A = "a" * 40
B = "b" * 40
C = "c" * 40
MB1 = "1" * 40
MB2 = "2" * 40
BASE = "9" * 40
Doc = dict[str, Any]
GONE = (
    "diff --git a/d b/d\ndeleted file mode 100644\nindex 1..0000000\n"
    "--- a/d\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
)
ADDED = (
    "diff --git a/n b/n\nnew file mode 100644\nindex 0000000..1\n"
    "--- /dev/null\n+++ b/n\n@@ -0,0 +1 @@\n+x\n"
)
RENAME_100 = (
    "diff --git a/old.py b/new.py\nsimilarity index 100%\nrename from old.py\nrename to new.py\n"
)


def finding(
    fid: str, path: str, line: int | None, *, start: int | None = None, side: str | None = None
) -> Doc:
    out: Doc = {"id": fid, "path": path, "line": line, "body": f"body {fid}"}
    if start is not None:
        out["start_line"] = start
    if side is not None:
        out["side"] = side
    return out


def make_draft(
    findings: list[Doc], unanchored: list[Doc] | None = None, merge_base: str | None = MB1
) -> Draft:
    doc: Doc = {
        "schema_version": 1,
        "draft_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "pr": "github.com/example/widgets#7",
        "anchor": {
            "head_sha": A,
            "base_ref": "main",
            "base_sha": BASE,
            "merge_base_sha": merge_base,
        },
        "event": "request_changes",
        "body": "summary",
        "findings": findings,
    }
    if unanchored is not None:
        doc["unanchored"] = unanchored
    assert validate_document(doc) == []
    return Draft(doc, "0" * 64)


def hunk_diff(path: str, hunks: str, new: str | None = None) -> str:
    old, name = path, new or path
    return f"diff --git a/{old} b/{name}\nindex 1..2 100644\n--- a/{old}\n+++ b/{name}\n{hunks}"


def run(
    draft: Draft,
    diff: str | None = "",
    *,
    head: str = B,
    merge_base: str | None = MB1,
) -> Any:
    mapper = DiffMapper.unavailable() if diff is None else DiffMapper.from_text(diff)
    return reanchor(
        draft,
        head_sha=head,
        base_ref="main2",
        base_sha=C,
        merge_base_sha=merge_base,
        mapper=mapper,
    )


def reasons(result: Any) -> dict[str, str]:
    return {m.finding["id"]: m.reason for m in result.moved}


def by_id(doc: Doc) -> dict[str, Doc]:
    return {f["id"]: f for f in doc["findings"]}


# --- anchor, ids and carry-over -----------------------------------------------------------


def test_anchor_is_set_to_b_and_everything_else_is_kept() -> None:
    draft = make_draft([finding("f1", "a.py", 3)])
    draft.data["meta"] = {"k": [1, 2]}
    out = run(draft).draft
    assert out["anchor"] == {
        "head_sha": B,
        "base_ref": "main2",
        "base_sha": C,
        "merge_base_sha": None,
    }
    for key in ("schema_version", "draft_id", "pr", "event", "body", "meta"):
        assert out[key] == draft.data[key]


def test_source_draft_is_not_modified() -> None:
    draft = make_draft([finding("f1", "a.py", 3), finding("f2", "a.py", 4, side="left")])
    before = copy.deepcopy(draft.data)
    run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n"), merge_base=MB2)
    assert draft.data == before


def test_merge_base_is_recorded_only_when_a_left_finding_ran_the_check() -> None:
    without = make_draft([finding("f1", "a.py", 3)])
    assert run(without, merge_base=MB1).draft["anchor"]["merge_base_sha"] is None
    with_left = make_draft([finding("f1", "a.py", 3, side="left")])
    assert run(with_left, merge_base=MB1).draft["anchor"]["merge_base_sha"] == MB1
    assert run(with_left, merge_base=None).draft["anchor"]["merge_base_sha"] is None


def test_unanchored_entries_stay_first_and_new_ones_follow_in_draft_order() -> None:
    old = {"finding": finding("u1", "z.py", 2), "head_sha": C, "reason": "changed"}
    draft = make_draft(
        [finding("f1", "a.py", 3), finding("f2", "a.py", 5), finding("f3", "a.py", 9)],
        [old],
    )
    out = run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n@@ -9 +9 @@\n-x\n+y\n")).draft
    assert [f["id"] for f in out["findings"]] == ["f2"]
    assert out["unanchored"][0] == old
    assert [(u["finding"]["id"], u["head_sha"], u["reason"]) for u in out["unanchored"][1:]] == [
        ("f1", A, "changed"),
        ("f3", A, "changed"),
    ]


def test_unanchored_key_is_absent_when_nothing_moved_and_none_existed() -> None:
    out = run(make_draft([finding("f1", "a.py", 3)])).draft
    assert "unanchored" not in out
    kept = make_draft([finding("f1", "a.py", 3)], [])
    assert run(kept).draft["unanchored"] == []


def test_unanchored_entry_is_never_mapped() -> None:
    old = {"finding": finding("u1", "a.py", 3), "head_sha": C, "reason": "changed"}
    draft = make_draft([], [old])
    out = run(draft, hunk_diff("a.py", "@@ -1 +1,5 @@\n-x\n+1\n+2\n+3\n+4\n+5\n"), merge_base=MB2)
    assert out.draft["unanchored"] == [old]


def test_moved_finding_is_the_original_object_with_head_a() -> None:
    draft = make_draft([finding("f1", "a.py", 3, start=2)])
    out = run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n"))
    entry = out.draft["unanchored"][0]
    assert entry == {"finding": draft.data["findings"][0], "head_sha": A, "reason": "changed"}
    assert [(m.finding["id"], m.reason) for m in out.moved] == [("f1", "changed")]


# --- right side ---------------------------------------------------------------------------


def test_b_equal_to_a_keeps_right_side_lines() -> None:
    draft = make_draft([finding("f1", "a.py", 3, start=1), finding("f2", "b.py", 7)])
    out = run(draft, "", head=A)
    assert out.moved == ()
    assert out.draft["findings"] == draft.data["findings"]
    assert out.draft["anchor"]["head_sha"] == A


def test_b_equal_to_a_ignores_any_mapper() -> None:
    draft = make_draft([finding("f1", "a.py", 3)])
    out = run(draft, None, head=A)  # an unavailable mapper must not move anything
    assert out.moved == ()
    out = run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n"), head=A)
    assert out.moved == ()


def test_insert_above_shifts_lines_and_range() -> None:
    draft = make_draft([finding("f1", "a.py", 10), finding("f2", "a.py", 12, start=10)])
    out = run(draft, hunk_diff("a.py", "@@ -2,0 +3,2 @@\n+n1\n+n2\n"))
    got = by_id(out.draft)
    assert (got["f1"]["line"], got["f2"]["line"], got["f2"]["start_line"]) == (12, 14, 12)
    assert "start_line" not in got["f1"]


def test_changed_and_removed_lines_move_changed() -> None:
    draft = make_draft([finding("f1", "a.py", 3), finding("f2", "a.py", 3, start=2)])
    out = run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n"))
    assert reasons(out) == {"f1": "changed", "f2": "changed"}


def test_file_removed_and_rename_without_detection() -> None:
    gone = GONE
    draft = make_draft([finding("f1", "d", 1)])
    assert reasons(run(draft, gone)) == {"f1": "file_removed"}


def test_rename_is_followed_and_lines_map() -> None:
    diff = (
        "diff --git a/old.py b/new.py\nsimilarity index 90%\nrename from old.py\nrename to new.py\n"
        "index 1..2 100644\n--- a/old.py\n+++ b/new.py\n@@ -1,0 +2 @@\n+added\n"
    )
    draft = make_draft([finding("f1", "old.py", 5), finding("f2", "old.py", None)])
    out = run(draft, diff)
    got = by_id(out.draft)
    assert (got["f1"]["path"], got["f1"]["line"]) == ("new.py", 6)
    assert (got["f2"]["path"], got["f2"]["line"]) == ("new.py", None)


def test_path_added_at_b_cannot_have_had_lines() -> None:
    added = ADDED
    assert reasons(run(make_draft([finding("f1", "n", 1)]), added)) == {"f1": "changed"}


def test_binary_file_moves_changed() -> None:
    diff = (
        "diff --git a/b.dat b/b.dat\nindex 1..2 100644\nBinary files a/b.dat and b/b.dat differ\n"
    )
    assert reasons(run(make_draft([finding("f1", "b.dat", 1)]), diff)) == {"f1": "changed"}


def test_unavailable_diff_moves_every_right_side_finding() -> None:
    draft = make_draft(
        [
            finding("f1", "a.py", 3),
            finding("f2", "b.py", 2, start=1),
            finding("f3", "c.py", None),
            finding("f4", "d.py", 4, side="left"),
        ]
    )
    out = run(draft, None, merge_base=MB1)
    assert reasons(out) == {"f1": "diff_unavailable", "f2": "diff_unavailable"}
    assert [f["id"] for f in out.draft["findings"]] == ["f3", "f4"]


@pytest.mark.parametrize(
    "diff",
    [
        "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-x\n+y\n@@ -9 +3 @@\n-x\n+y\n",
        "diff --git a/f b/f\nindex 1..2 100644\n--- a/f\n+++ b/f\n",
        "garbage that is not a diff\n",
    ],
)
def test_ambiguous_diff_moves_every_right_side_finding(diff: str) -> None:
    draft = make_draft([finding("f1", "a", 3), finding("f2", "other", 1)])
    assert set(reasons(run(draft, diff)).values()) <= {"diff_unavailable"}
    assert len(run(draft, diff).moved) == 2


def test_literal_marker_path_against_real_bidi_path_moves_diff_unavailable() -> None:
    real = "a‮b.py"
    diff = hunk_diff(real, "@@ -3 +3 @@\n-x\n+y\n")
    draft = make_draft([finding("f1", "a<U+202E>b.py", 9)])
    assert reasons(run(draft, diff)) == {"f1": "diff_unavailable"}


def test_unchanged_file_keeps_its_original_path_string() -> None:
    odd = "x‮y.py"
    draft = make_draft([finding("f1", odd, 4)])
    out = run(draft, hunk_diff("other.py", "@@ -3 +3 @@\n-x\n+y\n"))
    assert by_id(out.draft)["f1"]["path"] == odd


def test_huge_and_boundary_lines() -> None:
    draft = make_draft(
        [
            finding("f1", "a.py", 2**31 - 2),
            finding("f2", "a.py", 1),
            finding("f3", "a.py", 2**31 - 1),
        ]
    )
    out = run(draft, hunk_diff("a.py", "@@ -1,0 +2 @@\n+n\n"))
    assert by_id(out.draft)["f2"]["line"] == 1
    assert by_id(out.draft)["f1"]["line"] == 2**31 - 1  # unknown length: the pre-check decides
    # shifted past the largest line a draft can hold: moves, so the new draft stays valid
    assert [m.finding["id"] for m in out.moved] == ["f3"]
    assert validate_document(out.draft) == []


def test_file_level_findings_never_move_or_validate() -> None:
    gone = GONE
    draft = make_draft([finding("f1", "d", None), finding("f2", "<U+1>", None)])
    out = run(draft, gone)
    assert out.moved == ()
    assert by_id(out.draft)["f1"]["path"] == "d"
    assert run(draft, None).moved == ()


# --- left side ----------------------------------------------------------------------------


def test_left_side_ok_needs_both_known_and_equal() -> None:
    assert left_side_ok(MB1, MB1)
    assert not left_side_ok(MB1, MB2)
    assert not left_side_ok(None, MB1)
    assert not left_side_ok(MB1, None)
    assert not left_side_ok(None, None)


def test_left_side_findings_keep_lines_when_merge_bases_match() -> None:
    draft = make_draft([finding("f1", "a.py", 3, side="left", start=2)])
    out = run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n"), merge_base=MB1)
    assert out.moved == ()
    f = by_id(out.draft)["f1"]
    assert (f["line"], f["start_line"], f["side"]) == (3, 2, "left")


@pytest.mark.parametrize("current", [MB2, None])
@pytest.mark.parametrize("recorded", [MB1, None])
def test_left_side_moves_base_changed_unless_both_known_and_equal(
    current: str | None, recorded: str | None
) -> None:
    if current == recorded == MB1:
        pytest.skip("equal")
    draft = make_draft([finding("f1", "a.py", 3, side="left")], merge_base=recorded)
    assert reasons(run(draft, "", merge_base=current)) == {"f1": BASE_CHANGED}


def test_base_changed_applies_with_b_equal_to_a() -> None:
    draft = make_draft([finding("f1", "a.py", 3, side="left"), finding("f2", "a.py", 3)])
    out = run(draft, "", head=A, merge_base=MB2)
    assert reasons(out) == {"f1": "base_changed"}
    assert [f["id"] for f in out.draft["findings"]] == ["f2"]


def test_left_finding_ignores_the_right_side_line_mapping() -> None:
    # The same line is removed by the A-to-B diff; a left line is a line of the base, so it stays.
    draft = make_draft([finding("f1", "a.py", 3, side="left"), finding("f2", "a.py", 3)])
    out = run(draft, hunk_diff("a.py", "@@ -3 +3 @@\n-x\n+y\n"))
    assert reasons(out) == {"f2": "changed"}
    assert by_id(out.draft)["f1"]["line"] == 3


def test_left_finding_on_a_deleted_file_moves_file_removed() -> None:
    gone = GONE
    draft = make_draft([finding("f1", "d", 1, side="left")])
    assert reasons(run(draft, gone)) == {"f1": "file_removed"}


def test_base_changed_wins_over_file_removed() -> None:
    gone = GONE
    draft = make_draft([finding("f1", "d", 1, side="left")])
    assert reasons(run(draft, gone, merge_base=MB2)) == {"f1": "base_changed"}


def test_left_finding_does_not_follow_a_rename() -> None:
    draft = make_draft([finding("f1", "old.py", 8, side="left"), finding("f2", "old.py", None)])
    out = run(draft, RENAME_100)
    assert reasons(out) == {"f1": "file_removed"}
    assert by_id(out.draft)["f2"]["path"] == "new.py"


def test_base_changed_wins_over_a_rename_for_left_findings() -> None:
    draft = make_draft([finding("f1", "old.py", 8, side="left")])
    assert reasons(run(draft, RENAME_100, merge_base=MB2)) == {"f1": BASE_CHANGED}


def test_left_finding_on_an_unchanged_file_keeps_its_path_beside_a_rename() -> None:
    draft = make_draft([finding("f1", "keep.py", 8, side="left")])
    out = run(draft, RENAME_100)
    assert out.moved == ()
    assert by_id(out.draft)["f1"]["path"] == "keep.py"


def test_left_finding_with_unavailable_diff_follows_step_three_only() -> None:
    draft = make_draft([finding("f1", "old.py", 8, side="left")])
    out = run(draft, None, merge_base=MB1)
    assert out.moved == ()
    assert by_id(out.draft)["f1"]["path"] == "old.py"


# --- compare handling ---------------------------------------------------------------------


def compare(status: str, diff: str | None, to_sha: str = B) -> Doc:
    return {
        "pr": "github.com/o/r#1",
        "from_sha": A,
        "to_sha": to_sha,
        "status": status,
        "diff": diff,
    }


def test_compare_ahead_builds_a_mapper() -> None:
    diff = hunk_diff("a.py", "@@ -2,0 +3 @@\n+n\n")
    mapper = mapper_from_compare(compare("ahead", diff), head_sha=B, from_sha=A)
    assert mapper.map_right("a.py", 5).line == 6  # type: ignore[union-attr]


def test_compare_identical_keeps_lines() -> None:
    mapper = mapper_from_compare(compare("identical", ""), head_sha=B, from_sha=A)
    assert mapper.available
    assert mapper.map_right("a.py", 5).line == 5  # type: ignore[union-attr]


@pytest.mark.parametrize("status", ["behind", "diverged", "unavailable"])
def test_compare_without_a_diff_is_unavailable(status: str) -> None:
    mapper = mapper_from_compare(compare(status, None), head_sha=B, from_sha=A)
    assert not mapper.available


def test_too_large_compare_is_unavailable() -> None:
    assert not mapper_from_compare(None, head_sha=B, from_sha=A).available


def test_compare_requires_from_sha() -> None:
    with pytest.raises(TypeError):
        mapper_from_compare(compare("ahead", ""), head_sha=B)  # type: ignore[call-arg]  # pyright: ignore


def test_force_push_moves_every_right_side_finding() -> None:
    draft = make_draft([finding("f1", "a.py", 3), finding("f2", "b.py", 4, start=2)])
    mapper = mapper_from_compare(compare("diverged", None), head_sha=B, from_sha=A)
    out = reanchor(
        draft, head_sha=B, base_ref="main", base_sha=C, merge_base_sha=None, mapper=mapper
    )
    assert reasons(out) == {"f1": "diff_unavailable", "f2": "diff_unavailable"}


def error_document(outcome: Any) -> Doc:
    assert outcome.document is not None
    validate("pr-comment/cli/error.json", outcome.document)
    return outcome.document["error"]


def test_compare_to_sha_must_equal_b() -> None:
    with pytest.raises(StaleHeadError) as info:
        mapper_from_compare(compare("ahead", "", to_sha=C), head_sha=B, from_sha=A)
    assert info.value.head_sha == C
    outcome = info.value.outcome()
    assert outcome.exit_code == 20
    error = error_document(outcome)
    assert error["reason"] == "stale_head"
    assert error["details"] == {"head_sha": C}


def test_compare_to_sha_is_lowercased_before_the_check() -> None:
    shape = compare("identical", "", to_sha=B.upper())
    assert mapper_from_compare(shape, head_sha=B, from_sha=A).available
    with pytest.raises(StaleHeadError) as info:
        mapper_from_compare(compare("ahead", "", to_sha=C.upper()), head_sha=B, from_sha=A)
    assert error_document(info.value.outcome())["details"] == {"head_sha": C}


def test_compare_to_sha_is_checked_even_without_a_diff() -> None:
    with pytest.raises(StaleHeadError):
        mapper_from_compare(compare("diverged", None, to_sha=C), head_sha=B, from_sha=A)


@pytest.mark.parametrize(
    "shape",
    [
        {},
        {"to_sha": None},
        {"to_sha": 5},
        {"to_sha": "c" * 39},
        {"to_sha": "g" * 40},
        {"to_sha": B, "diff": 7},
        {"to_sha": C, "diff": 7},
        {"to_sha": B, "diff": b"x"},
        [],
        "x",
        5,
    ],
)
def test_malformed_compare_fails_closed_with_a_contract_error(shape: Any) -> None:
    with pytest.raises(CompareContractError) as info:
        mapper_from_compare(shape, head_sha=B, from_sha=A)
    outcome = info.value.outcome()
    assert outcome.exit_code == 12
    error = error_document(outcome)
    assert error["code"] == "lower_contract_mismatch"
    assert error["details"] == {"command": "compare", "lower_version": None}


@pytest.mark.parametrize("from_sha", [None, 5, "a" * 39, C, "g" * 40])
def test_compare_from_sha_must_equal_a_when_a_is_given(from_sha: Any) -> None:
    shape = compare("ahead", "")
    shape["from_sha"] = from_sha
    with pytest.raises(CompareContractError):
        mapper_from_compare(shape, head_sha=B, from_sha=A)


def test_compare_from_sha_matching_a_is_accepted_in_either_case() -> None:
    shape = compare("ahead", "")
    shape["from_sha"] = A.upper()
    assert mapper_from_compare(shape, head_sha=B, from_sha=A).available


# --- lines past the end of A -------------------------------------------------------------


@pytest.mark.parametrize("changed", [False, True])
def test_line_past_the_end_of_a_is_not_checked_and_lands_past_the_end_of_b(
    tmp_path: Path, changed: bool
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, home, "init", "-q", "-b", "main")
    old = [f"line{i}" for i in range(1, 11)]
    (repo / "f.txt").write_text("\n".join(old) + "\n")
    git(repo, home, "add", "f.txt")
    git(repo, home, "commit", "-q", "-m", "a")
    sha_a = git(repo, home, "rev-parse", "HEAD").strip()
    new = ["new1", "new2", *old[1:]] if changed else old
    (repo / "f.txt").write_text("\n".join(new) + "\n")
    git(repo, home, "commit", "-q", "-a", "--allow-empty", "-m", "b")
    sha_b = git(repo, home, "rev-parse", "HEAD").strip()
    diff = git(repo, home, "diff", sha_a, sha_b)
    draft = make_draft([finding("f1", "f.txt", 15)])
    out = reanchor(
        draft,
        head_sha=sha_b,
        base_ref="main",
        base_sha=C,
        merge_base_sha=None,
        mapper=DiffMapper.from_text(diff),
    )
    placed = by_id(out.draft)["f1"]
    assert placed["line"] > len(new)


# --- input checks -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "B" * 40, "b" * 39, "g" * 40, "b" * 40 + "\n"])
def test_head_sha_must_be_40_lowercase_hex(bad: str) -> None:
    with pytest.raises(ValueError, match="head_sha"):
        run(make_draft([]), head=bad)


def test_merge_base_must_be_a_sha_or_none() -> None:
    with pytest.raises(ValueError, match="merge_base_sha"):
        run(make_draft([finding("f1", "a", 1, side="left")]), merge_base="nope")


# --- determinism and schema ---------------------------------------------------------------


def canonical(doc: Doc) -> str:
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_result_is_deterministic_and_valid() -> None:
    draft = make_draft(
        [
            finding("f1", "a.py", 10),
            finding("f2", "a.py", 3),
            finding("f3", "a.py", 4, side="left"),
            finding("f4", "b.py", None),
        ],
        [{"finding": finding("u1", "x", 1), "head_sha": C, "reason": "changed"}],
    )
    diff = hunk_diff("a.py", "@@ -2,0 +3,2 @@\n+n1\n+n2\n@@ -3 +5 @@\n-x\n+y\n")
    first = run(draft, diff, merge_base=MB2)
    second = run(copy.deepcopy(draft), diff, merge_base=MB2)
    assert first.draft == second.draft
    assert json.dumps(first.draft) == json.dumps(second.draft)  # key order too
    assert validate_document(first.draft) == []
    validate("pr-comment/review-draft.json", first.draft)


def test_review_result_schema_accepts_the_reanchored_draft() -> None:
    draft = make_draft([finding("f1", "a.py", 10)])
    out = run(draft, hunk_diff("a.py", "@@ -2,0 +3 @@\n+n\n"))
    with pytest.raises(jsonschema.ValidationError):
        validate("pr-comment/review-draft.json", {**out.draft, "anchor": {}})
    validate("pr-comment/review-draft.json", out.draft)


# --- property: every finding exactly once -------------------------------------------------


def random_draft(rng: random.Random) -> Draft:
    paths = ["a.py", "b.py", "d", "old.py", "s p/é.txt"]
    findings: list[Doc] = []
    for i in range(rng.randint(0, 12)):
        line = rng.choice([None, 1, 2, 3, 5, 8, 13, 40, 2**31 - 1])
        start = None
        if line is not None and line > 1 and rng.random() < 0.3:
            start = rng.randint(1, min(line - 1, 12))
        side = rng.choice([None, "right", "left"]) if line is not None else None
        findings.append(finding(f"f{i}", rng.choice(paths), line, start=start, side=side))
    unanchored = [
        {
            "finding": finding(
                f"u{i}",
                rng.choice(paths),
                rng.choice([None, 2]),
            ),
            "head_sha": C,
            "reason": "changed",
        }
        for i in range(rng.randint(0, 3))
    ]
    return make_draft(findings, unanchored or None, merge_base=rng.choice([MB1, None]))


DIFFS: list[str | None] = [
    None,
    "",
    hunk_diff("a.py", "@@ -2,0 +3,2 @@\n+n1\n+n2\n@@ -5 +7 @@\n-x\n+y\n"),
    GONE,
    RENAME_100,
    "diff --git a/b.py b/b.py\nindex 1..2 100644\nBinary files a/b.py and b/b.py differ\n",
]


@pytest.mark.parametrize("seed", range(200))
def test_every_finding_appears_exactly_once_with_a_stable_id(seed: int) -> None:
    rng = random.Random(seed)
    draft = random_draft(rng)
    diff = rng.choice(DIFFS)
    out = run(draft, diff, head=rng.choice([A, B]), merge_base=rng.choice([MB1, MB2, None]))
    source_ids = [f["id"] for f in draft.data["findings"]] + [
        u["finding"]["id"] for u in draft.data.get("unanchored", [])
    ]
    got_ids = [f["id"] for f in out.draft["findings"]] + [
        u["finding"]["id"] for u in out.draft.get("unanchored", [])
    ]
    assert sorted(got_ids) == sorted(source_ids)
    assert len(set(got_ids)) == len(got_ids)
    assert validate_document(out.draft) == []
    # kept findings keep their relative order, and only path and lines can differ
    kept = [f["id"] for f in out.draft["findings"]]
    assert kept == [i for i in (f["id"] for f in draft.data["findings"]) if i in kept]
    src = {f["id"]: f for f in draft.data["findings"]}
    for f in out.draft["findings"]:
        for key in set(f) | set(src[f["id"]]):
            if key not in ("path", "line", "start_line"):
                assert f.get(key) == src[f["id"]].get(key)
    # source unanchored entries lead the new list, untouched
    old = draft.data.get("unanchored", [])
    assert out.draft.get("unanchored", [])[: len(old)] == old


# --- oracle: real git diff ----------------------------------------------------------------


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }


def git(repo: Path, home: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        env=git_env(home),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return done.stdout


def mutate(rng: random.Random, lines: list[str], counter: list[int]) -> list[str]:
    out = list(lines)
    for _ in range(rng.randint(1, 6)):
        counter[0] += 1
        new = f"new{counter[0]}"
        op = rng.choice(["insert", "delete", "replace", "replace"])
        pos = rng.randrange(len(out) + 1) if op == "insert" else rng.randrange(len(out))
        if op == "insert":
            out.insert(pos, new)
        elif op == "delete" and len(out) > 3:
            del out[pos]
        else:
            out[pos] = new
    return out


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("context", [0, 1, 3])
def test_oracle_git_diff_every_mapped_finding_lands_on_the_same_text(
    tmp_path: Path, seed: int, context: int
) -> None:
    rng = random.Random(seed)
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, home, "init", "-q", "-b", "main")
    old = [f"line{i}" for i in range(1, rng.randint(8, 40))]
    new = mutate(rng, old, [0])
    (repo / "f.txt").write_text("\n".join(old) + "\n")
    git(repo, home, "add", "f.txt")
    git(repo, home, "commit", "-q", "-m", "a")
    sha_a = git(repo, home, "rev-parse", "HEAD").strip()
    (repo / "f.txt").write_text("\n".join(new) + "\n")
    git(repo, home, "commit", "-q", "-am", "b")
    sha_b = git(repo, home, "rev-parse", "HEAD").strip()
    diff = git(repo, home, "diff", f"-U{context}", sha_a, sha_b)

    findings = [finding(f"s{n}", "f.txt", n) for n in range(1, len(old) + 1)]
    ranges = [(s, e) for s in range(1, len(old)) for e in range(s + 1, min(len(old), s + 4) + 1)]
    for s, e in rng.sample(ranges, min(15, len(ranges))):
        findings.append(finding(f"r{s}_{e}", "f.txt", e, start=s))
    draft = make_draft(findings)
    out = reanchor(
        draft,
        head_sha=sha_b,
        base_ref="main",
        base_sha=C,
        merge_base_sha=None,
        mapper=DiffMapper.from_text(diff),
    )
    kept = by_id(out.draft)
    removed_lines = removed_by_git(old, new)
    for f in findings:
        lo = f.get("start_line", f["line"])
        span = range(lo, f["line"] + 1)
        if f["id"] in kept:
            m = kept[f["id"]]
            mapped_lo = m.get("start_line", m["line"])
            mapped = range(mapped_lo, m["line"] + 1)
            assert [new[i - 1] for i in mapped] == [old[i - 1] for i in span]
        else:
            # exactness: a finding moves only when it touches a line the diff removed, or a
            # range has lines inserted inside it (not contiguous at B)
            assert reasons(out)[f["id"]] == "changed"
            touched = any(n in removed_lines for n in span)
            positions = [new.index(old[n - 1]) for n in span if n not in removed_lines]
            gapped = bool(positions) and positions != list(
                range(positions[0], positions[0] + len(positions))
            )
            assert touched or gapped, (seed, context, f["id"])


def removed_by_git(old: list[str], new: list[str]) -> set[int]:
    """Lines of ``old`` that are not part of a longest common subsequence with ``new``.

    Every old line has a unique text (``lineN`` are distinct, ``newN`` are new), so a line
    survives exactly when its text is still in ``new``, in order.
    """
    kept = [t for t in old if t in new]
    return {i + 1 for i, t in enumerate(old) if t not in kept}


def test_oracle_git_rename_and_delete(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, home, "init", "-q", "-b", "main")
    body = "".join(f"content line {i}\n" for i in range(1, 21))
    (repo / "old name.txt").write_text(body)
    (repo / "gone.txt").write_text("unrelated\n" * 5 + body[:30])
    git(repo, home, "add", ".")
    git(repo, home, "commit", "-q", "-m", "a")
    sha_a = git(repo, home, "rev-parse", "HEAD").strip()
    git(repo, home, "mv", "old name.txt", "new name.txt")
    (repo / "new name.txt").write_text("top\n" + body)
    git(repo, home, "rm", "-q", "gone.txt")
    git(repo, home, "commit", "-q", "-am", "b")
    sha_b = git(repo, home, "rev-parse", "HEAD").strip()
    diff = git(repo, home, "diff", "-M", sha_a, sha_b)
    draft = make_draft(
        [
            finding("r", "old name.txt", 5),
            finding("l", "old name.txt", 5, side="left"),
            finding("fl", "old name.txt", None),
            finding("g", "gone.txt", 2),
            finding("gl", "gone.txt", 2, side="left"),
        ]
    )
    out = reanchor(
        draft,
        head_sha=sha_b,
        base_ref="main",
        base_sha=C,
        merge_base_sha=MB1,
        mapper=DiffMapper.from_text(diff),
    )
    got = by_id(out.draft)
    assert (got["r"]["path"], got["r"]["line"]) == ("new name.txt", 6)
    assert got["fl"]["path"] == "new name.txt"
    assert reasons(out) == {"l": "file_removed", "g": "file_removed", "gl": "file_removed"}


def test_oracle_git_rename_onto_a_deleted_path_does_not_move_a_left_comment_to_wrong_code(
    tmp_path: Path,
) -> None:
    # Base has X and Y. A deletes Y and edits X. B does `git mv X Y`. A-to-B reports X renamed
    # to Y, but base-to-B shows X deleted and Y modified: base X line 5 is not base Y line 5.
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, home, "init", "-q", "-b", "main")
    x = "".join(f"x content {i}\n" for i in range(1, 21))
    y = "".join(f"y content {i}\n" for i in range(1, 21))
    (repo / "X").write_text(x)
    (repo / "Y").write_text(y)
    git(repo, home, "add", ".")
    git(repo, home, "commit", "-q", "-m", "base")
    git(repo, home, "rm", "-q", "Y")
    (repo / "X").write_text(x.replace("x content 15\n", "x edited\n"))
    git(repo, home, "commit", "-q", "-am", "a")
    sha_a = git(repo, home, "rev-parse", "HEAD").strip()
    git(repo, home, "mv", "X", "Y")
    git(repo, home, "commit", "-q", "-m", "b")
    sha_b = git(repo, home, "rev-parse", "HEAD").strip()
    diff = git(repo, home, "diff", "-M", sha_a, sha_b)
    assert "rename from X" in diff
    draft = make_draft([finding("l", "X", 5, side="left"), finding("fl", "X", None)])
    out = reanchor(
        draft,
        head_sha=sha_b,
        base_ref="main",
        base_sha=C,
        merge_base_sha=MB1,
        mapper=DiffMapper.from_text(diff),
    )
    assert reasons(out) == {"l": "file_removed"}
    assert by_id(out.draft)["fl"]["path"] == "Y"


SPIKE_SCENARIO = Path(__file__).resolve().parent / "fixtures" / "spike_a" / "scenario"


def test_spike_a_base_merge_left_findings_move_base_changed() -> None:
    # After the base merge the head-to-head diff shifts f.txt; a left line mapped through it would
    # land on a valid but wrong line. The merge-base changed, so the left finding must move.
    diff = (SPIKE_SCENARIO / "A...B.diff").read_text()
    draft = make_draft(
        [
            finding("left", "f.txt", 18, side="left"),
            finding("right", "f.txt", 20),
            finding("right-main", "f.txt", 18),
        ]
    )
    out = run(draft, diff, merge_base=MB2)
    assert reasons(out)["left"] == "base_changed"
    assert by_id(out.draft)["right"]["line"] == 22
