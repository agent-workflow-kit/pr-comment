"""Tests for right-side mapping through a parsed diff (docs/interface.md, Re-anchoring step 2)."""

from __future__ import annotations

import difflib
import json
import random
from pathlib import Path

import pytest

from pr_comment.diffmap import (
    AmbiguousDiffError,
    DiffMapper,
    Mapped,
    Moved,
    MovedReason,
    parse_diff,
    printed_path,
)

SPIKE_A = Path(__file__).resolve().parent / "fixtures" / "spike_a"
SIZES = ("U0", "U1", "U3", "U10")
CASES = sorted(p for p in SPIKE_A.iterdir() if (p / "expected.json").is_file())

# The spike called a removed line "deleted"; the interface calls it "changed".
SPIKE_REASONS = {
    "changed": MovedReason.CHANGED,
    "deleted": MovedReason.CHANGED,
    "file_removed": MovedReason.FILE_REMOVED,
}


def mapper_for(text: str) -> DiffMapper:
    return DiffMapper.from_text(text)


def case_text(case: Path, size: str) -> str:
    return (case / f"{size}.diff").read_bytes().decode("utf-8")


def modified(old: str, new: str, hunks: str, *, extra: str = "") -> str:
    return (
        f"diff --git a/{old} b/{new}\n{extra}index 1..2 100644\n--- a/{old}\n+++ b/{new}\n{hunks}"
    )


# --- spike A: 22 hand cases, 52 findings, every context size ------------------------------


def test_spike_a_has_22_cases_and_every_recorded_finding() -> None:
    cases = [c for c in CASES if c.name[:2].isdigit()]
    assert len(cases) == 22
    # The design counts 52 findings; the recorded expected.json files hold 55.
    assert sum(len(json.loads((c / "expected.json").read_text())) for c in cases) >= 52


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("case", CASES, ids=lambda p: p.name)
def test_spike_a_case_maps_as_recorded(case: Path, size: str) -> None:
    mapper = mapper_for(case_text(case, size))
    for entry in json.loads((case / "expected.json").read_text()):
        finding, expected = entry["finding"], entry["expected"]
        assert finding["side"] == "right"
        got = mapper.map_right(finding["path"], finding["line"], finding.get("start_line"))
        if "unmapped" in expected:
            assert got == Moved(SPIKE_REASONS[expected["unmapped"]]), (case.name, size, finding)
        else:
            assert got == Mapped(expected["path"], expected["line"], expected.get("start_line")), (
                case.name,
                size,
                finding,
            )


# --- hand-built edge cases ----------------------------------------------------------------


def test_file_absent_from_diff_keeps_lines() -> None:
    mapper = mapper_for(modified("a.txt", "a.txt", "@@ -1 +1 @@\n-x\n+y\n"))
    assert mapper.map_right("other.txt", 7) == Mapped("other.txt", 7, None)


def test_identity_mapper_keeps_everything() -> None:
    assert mapper_for("").map_right("f", 3, 1) == Mapped("f", 3, 1)


def test_unavailable_mapper_moves_everything() -> None:
    for mapper in (DiffMapper.from_text(None), DiffMapper.unavailable()):
        assert mapper.map_right("f", 3) == Moved(MovedReason.DIFF_UNAVAILABLE)
        assert not mapper.available


def test_ambiguous_diff_is_diff_unavailable() -> None:
    dup = modified("a", "a", "@@ -1 +1 @@\n-x\n+y\n") * 2
    mapper = mapper_for(dup)
    assert mapper.map_right("a", 1) == Moved(MovedReason.DIFF_UNAVAILABLE)
    with pytest.raises(AmbiguousDiffError):
        parse_diff(dup)


def test_context_line_inside_hunk_maps_and_removed_line_moves() -> None:
    hunk = "@@ -4,5 +4,6 @@\n c4\n-c5\n+n5\n+n5b\n c6\n c7\n c8\n"
    mapper = mapper_for(modified("f", "f", hunk))
    assert mapper.map_right("f", 4) == Mapped("f", 4, None)
    assert mapper.map_right("f", 5) == Moved(MovedReason.CHANGED)
    assert mapper.map_right("f", 6) == Mapped("f", 7, None)
    assert mapper.map_right("f", 8) == Mapped("f", 9, None)
    assert mapper.map_right("f", 9) == Mapped("f", 10, None)
    assert mapper.map_right("f", 3) == Mapped("f", 3, None)


def test_pure_insertion_hunk_boundaries() -> None:
    # Two lines inserted after old line 5 (zero-length old side).
    mapper = mapper_for(modified("f", "f", "@@ -5,0 +6,2 @@\n+i1\n+i2\n"))
    assert mapper.map_right("f", 5) == Mapped("f", 5, None)
    assert mapper.map_right("f", 6) == Mapped("f", 8, None)
    # A range across the insertion point is not contiguous after the edit.
    assert mapper.map_right("f", 6, 5) == Moved(MovedReason.CHANGED)
    assert mapper.map_right("f", 5, 4) == Mapped("f", 5, 4)
    assert mapper.map_right("f", 8, 6) == Mapped("f", 10, 8)


def test_pure_deletion_hunk_boundaries() -> None:
    mapper = mapper_for(modified("f", "f", "@@ -5,2 +4,0 @@\n-a\n-b\n"))
    assert mapper.map_right("f", 4) == Mapped("f", 4, None)
    assert mapper.map_right("f", 5) == Moved(MovedReason.CHANGED)
    assert mapper.map_right("f", 6) == Moved(MovedReason.CHANGED)
    assert mapper.map_right("f", 7) == Mapped("f", 5, None)
    # Lines 4 and 7 both map, but a range over the removed lines does not.
    assert mapper.map_right("f", 7, 4) == Moved(MovedReason.CHANGED)


def test_replace_one_for_one_does_not_make_a_range_look_contiguous() -> None:
    mapper = mapper_for(modified("f", "f", "@@ -10 +10 @@\n-old\n+new\n"))
    assert mapper.map_right("f", 11, 9) == Moved(MovedReason.CHANGED)
    assert mapper.map_right("f", 9, 9) == Mapped("f", 9, 9)


def test_range_after_several_hunks_shifts_both_ends() -> None:
    text = modified("f", "f", "@@ -2 +2,3 @@\n-x\n+a\n+b\n+c\n@@ -20,2 +21,0 @@\n-p\n-q\n")
    mapper = mapper_for(text)
    assert mapper.map_right("f", 12, 10) == Mapped("f", 14, 12)
    assert mapper.map_right("f", 30, 25) == Mapped("f", 30, 25)


def test_lines_beyond_every_hunk_and_far_past_the_file_shift_by_net() -> None:
    mapper = mapper_for(modified("f", "f", "@@ -1 +1,3 @@\n-x\n+a\n+b\n+c\n"))
    assert mapper.map_right("f", 1000000000) == Mapped("f", 1000000002, None)


def test_no_newline_markers_are_not_lines() -> None:
    marker = "\\ No newline at end of file\n"
    hunk = f"@@ -2,2 +2,3 @@\n l2\n-l3\n{marker}+l3\n+l4\n{marker}"
    mapper = mapper_for(modified("f", "f", hunk))
    assert mapper.map_right("f", 2) == Mapped("f", 2, None)
    assert mapper.map_right("f", 3) == Moved(MovedReason.CHANGED)


def test_crlf_content_does_not_matter() -> None:
    hunk = "@@ -1,2 +1,2 @@\n a\r\n-b\r\n+c\r\n"
    mapper = mapper_for(modified("f", "f", hunk))
    assert mapper.map_right("f", 1) == Mapped("f", 1, None)
    assert mapper.map_right("f", 2) == Moved(MovedReason.CHANGED)


def test_rename_with_edit_follows_path_and_lines() -> None:
    text = (
        "diff --git a/old.txt b/new.txt\nsimilarity index 97%\nrename from old.txt\n"
        "rename to new.txt\nindex 1..2 100644\n--- a/old.txt\n+++ b/new.txt\n"
        "@@ -1 +1,2 @@\n+top\n l1\n"
    )
    mapper = mapper_for(text)
    assert mapper.map_right("old.txt", 5) == Mapped("new.txt", 6, None)
    assert mapper.follow_file("old.txt") == ("new.txt", False)


def test_pure_rename_keeps_lines_under_the_new_path() -> None:
    text = (
        "diff --git a/old.txt b/new.txt\nsimilarity index 100%\nrename from old.txt\n"
        "rename to new.txt\n"
    )
    mapper = mapper_for(text)
    assert mapper.map_right("old.txt", 5, 3) == Mapped("new.txt", 5, 3)
    assert mapper.follow_file("old.txt") == ("new.txt", False)


def test_binary_change_moves_changed_and_binary_deletion_moves_file_removed() -> None:
    text = (
        "diff --git a/b.dat b/b.dat\nindex 1..2 100644\nBinary files a/b.dat and b/b.dat differ\n"
        "diff --git a/c.dat b/c.dat\ndeleted file mode 100644\nindex 1..0000000\n"
        "Binary files a/c.dat and /dev/null differ\n"
    )
    mapper = mapper_for(text)
    assert mapper.map_right("b.dat", 1) == Moved(MovedReason.CHANGED)
    assert mapper.map_right("c.dat", 1) == Moved(MovedReason.FILE_REMOVED)


def test_deleted_file_moves_file_removed_and_added_file_is_inconsistent() -> None:
    text = (
        "diff --git a/d b/d\ndeleted file mode 100644\nindex 1..0000000\n--- a/d\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
        "diff --git a/n b/n\nnew file mode 100644\nindex 0000000..1\n--- /dev/null\n+++ b/n\n"
        "@@ -0,0 +1 @@\n+y\n"
    )
    mapper = mapper_for(text)
    assert mapper.map_right("d", 1) == Moved(MovedReason.FILE_REMOVED)
    assert mapper.follow_file("d") == ("d", True)
    # A finding on a path that the diff says did not exist at A cannot be mapped.
    assert mapper.map_right("n", 1) == Moved(MovedReason.CHANGED)


def test_finding_on_a_rename_target_is_not_mapped_by_the_source_lines() -> None:
    text = "diff --git a/f b/g\nsimilarity index 100%\nrename from f\nrename to g\n"
    assert mapper_for(text).map_right("g", 3) == Moved(MovedReason.CHANGED)


def test_mode_only_section_keeps_lines() -> None:
    text = "diff --git a/f.txt b/f.txt\nold mode 100644\nnew mode 100755\n"
    assert mapper_for(text).map_right("f.txt", 7) == Mapped("f.txt", 7, None)


def test_quoted_non_ascii_and_spaced_paths() -> None:
    text = (
        'diff --git "a/s p/\\303\\251.txt" "b/s p/\\303\\251.txt"\nindex 1..2 100644\n'
        '--- "a/s p/\\303\\251.txt"\n+++ "b/s p/\\303\\251.txt"\n@@ -1 +1,2 @@\n+n\n x\n'
    )
    assert mapper_for(text).map_right("s p/é.txt", 1) == Mapped("s p/é.txt", 2, None)


def test_paths_compare_in_forgectls_printed_form() -> None:
    # git quotes U+202E as octal; forgectl prints findings' paths with a marker.
    text = (
        'diff --git "a/x\\342\\200\\256y" "b/x\\342\\200\\256y"\nindex 1..2 100644\n'
        '--- "a/x\\342\\200\\256y"\n+++ "b/x\\342\\200\\256y"\n@@ -1 +1,2 @@\n+n\n x\n'
    )
    mapper = mapper_for(text)
    assert mapper.map_right("x<U+202E>y", 1) == Mapped("x<U+202E>y", 2, None)
    # The raw character is not how a finding's path arrives; it simply is another path.
    assert mapper.map_right("xy", 1) == Mapped("xy", 1, None)


def test_a_literal_marker_path_colliding_with_a_real_one_fails_closed() -> None:
    text = (
        'diff --git "a/x\\342\\200\\256y" "b/x\\342\\200\\256y"\nindex 1..2 100644\n'
        '--- "a/x\\342\\200\\256y"\n+++ "b/x\\342\\200\\256y"\n@@ -1 +1 @@\n-a\n+b\n'
        "diff --git a/x<U+202E>y b/x<U+202E>y\nindex 1..2 100644\n"
        "--- a/x<U+202E>y\n+++ b/x<U+202E>y\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert mapper_for(text).map_right("x<U+202E>y", 1) == Moved(MovedReason.DIFF_UNAVAILABLE)


def test_hunk_starts_inconsistent_with_earlier_hunks_fail_closed() -> None:
    # Second hunk claims new_start 4 although the first added nothing net.
    text = modified("f", "f", "@@ -1 +1 @@\n-a\n+b\n@@ -10 +20 @@\n-c\n+d\n")
    assert mapper_for(text).map_right("f", 3) == Moved(MovedReason.DIFF_UNAVAILABLE)


def test_first_hunk_start_inconsistent_with_no_prior_change_fails_closed() -> None:
    text = modified("f", "f", "@@ -5 +9 @@\n-a\n+b\n")
    assert mapper_for(text).map_right("f", 1) == Moved(MovedReason.DIFF_UNAVAILABLE)


@pytest.mark.parametrize("line,start", [(0, None), (-1, None), (3, 4), (3, 0)])
def test_invalid_lines_are_rejected(line: int, start: int | None) -> None:
    with pytest.raises(ValueError):
        mapper_for("").map_right("f", line, start)


def test_printed_path_marks_what_forgectl_marks() -> None:
    assert printed_path("a/b.txt") == "a/b.txt"
    assert printed_path("é ü/日本") == "é ü/日本"
    assert printed_path("x‮y") == "x<U+202E>y"
    assert printed_path("x\ty\nz\rw") == "x<U+0009>y<U+000A>z<U+000D>w"
    assert printed_path("a​b️͏⠀") == "a<U+200B>b<U+FE0F><U+034F><U+2800>"
    assert printed_path("\U000e0100") == "<U+E0100>"
    assert printed_path("\ud800") == "<U+D800>"
    assert printed_path("x<U+202E>y") == "x<U+202E>y"  # idempotent


# --- property test against a reference mapper ---------------------------------------------


def git_style_diff(old: list[str], new: list[str], context: int) -> str:
    body = list(difflib.unified_diff(old, new, "a/f.txt", "b/f.txt", n=context, lineterm=""))
    if not body:
        return ""
    return "diff --git a/f.txt b/f.txt\nindex 1..2 100644\n" + "\n".join(body) + "\n"


def reference_map(old: list[str], new: list[str]) -> dict[int, int]:
    """Old line number -> new line number for every line the edit script keeps."""
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _ in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                mapping[i1 + k + 1] = j1 + k + 1
    return mapping


def random_pair(rng: random.Random) -> tuple[list[str], list[str]]:
    size = rng.randint(1, 30)
    old = [f"l{rng.randint(0, 6)}" for _ in range(size)]  # repeats make alignments ambiguous
    new = list(old)
    for _ in range(rng.randint(1, 6)):
        op = rng.choice(("insert", "delete", "replace"))
        pos = rng.randint(0, len(new))
        if op == "insert":
            new[pos:pos] = [f"n{rng.randint(0, 99)}" for _ in range(rng.randint(1, 4))]
        elif new:
            pos = min(pos, len(new) - 1)
            span = rng.randint(1, 3)
            new[pos : pos + span] = (
                [] if op == "delete" else [f"r{rng.randint(0, 99)}" for _ in range(span)]
            )
    return old, new or ["only"]


@pytest.mark.parametrize("seed", range(300))
def test_random_edits_agree_with_the_reference_mapper_at_every_context_size(seed: int) -> None:
    rng = random.Random(seed)
    old, new = random_pair(rng)
    # The reference alignment is the one difflib's opcodes (and so its diff) describe.
    kept = reference_map(old, new)
    for context in (0, 1, 3, 10):
        text = git_style_diff(old, new, context)
        mapper = mapper_for(text)
        assert mapper.available
        for n in range(1, len(old) + 6):
            got = mapper.map_right("f.txt", n)
            if n > len(old):
                assert got == Mapped("f.txt", n + len(new) - len(old), None)
            elif n in kept:
                assert got == Mapped("f.txt", kept[n], None), (seed, context, n)
            else:
                assert got == Moved(MovedReason.CHANGED), (seed, context, n)
        for _ in range(20):
            lo = rng.randint(1, len(old))
            hi = rng.randint(lo, len(old))  # lines past the old file have no reference
            mapped = [kept.get(k) for k in range(lo, hi + 1)]
            got = mapper.map_right("f.txt", hi, lo)
            values = [m for m in mapped if m is not None]
            contiguous = len(values) == len(mapped) and all(
                b - a == 1 for a, b in zip(values, values[1:], strict=False)
            )
            if contiguous:
                assert got == Mapped("f.txt", values[-1], values[0]), (seed, context, lo, hi)
            else:
                assert got == Moved(MovedReason.CHANGED), (seed, context, lo, hi)
