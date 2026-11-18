"""Tests for the unified-diff parser in ``pr_comment.diffmap`` (docs/interface.md, Re-anchoring)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pr_comment.diffmap import (
    AmbiguousDiffError,
    FileSection,
    HunkLine,
    SectionKind,
    parse_diff,
)

SPIKE_A = Path(__file__).resolve().parent / "fixtures" / "spike_a"


def text_diff(old: str, new: str, body: str = "@@ -1 +1 @@\n-a\n+b\n", *, tab: bool = False) -> str:
    """A modified-file section with raw ``---``/``+++`` tokens (already quoted if needed)."""
    t = "\t" if tab else ""
    return f"diff --git x y\nindex 1..2 100644\n--- {old}{t}\n+++ {new}{t}\n{body}"


def only(diff: str) -> FileSection:
    parsed = parse_diff(diff)
    assert len(parsed.sections) == 1
    return parsed.sections[0]


def ambiguous(diff: str) -> AmbiguousDiffError:
    with pytest.raises(AmbiguousDiffError) as info:
        parse_diff(diff)
    return info.value


# --- sections and kinds -------------------------------------------------------------------


def test_empty_text_has_no_sections() -> None:
    assert parse_diff("").sections == ()


def test_changed_section_with_hunk_lines() -> None:
    section = only(text_diff("a/f.txt", "b/f.txt"))
    assert section.kind is SectionKind.CHANGED
    assert (section.old_path, section.new_path) == ("f.txt", "f.txt")
    (hunk,) = section.hunks
    assert (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count) == (1, 1, 1, 1)
    assert hunk.lines == (HunkLine("-", "a"), HunkLine("+", "b"))


def test_added_section() -> None:
    diff = (
        "diff --git a/n b/n\nnew file mode 100644\nindex 0000000..1111111\n"
        "--- /dev/null\n+++ b/n\n@@ -0,0 +1,2 @@\n+x\n+y\n"
    )
    section = only(diff)
    assert section.kind is SectionKind.ADDED
    assert (section.old_path, section.new_path) == (None, "n")


def test_deleted_section() -> None:
    diff = (
        "diff --git a/n b/n\ndeleted file mode 100644\nindex 1111111..0000000\n"
        "--- a/n\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n"
    )
    section = only(diff)
    assert section.kind is SectionKind.DELETED
    assert (section.old_path, section.new_path) == ("n", None)


def test_pure_rename_has_no_hunks() -> None:
    diff = "diff --git a/o b/n\nsimilarity index 100%\nrename from o\nrename to n\n"
    section = only(diff)
    assert section.kind is SectionKind.RENAMED
    assert (section.old_path, section.new_path, section.hunks) == ("o", "n", ())


def test_rename_with_edit_reads_paths_from_both_sources() -> None:
    diff = (
        "diff --git a/o b/n\nsimilarity index 90%\nrename from o\nrename to n\n"
        "index 1..2 100644\n--- a/o\n+++ b/n\n@@ -1 +1 @@\n-a\n+b\n"
    )
    section = only(diff)
    assert section.kind is SectionKind.RENAMED
    assert (section.old_path, section.new_path) == ("o", "n")
    assert len(section.hunks) == 1


def test_rename_lines_disagreeing_with_dash_lines_is_ambiguous() -> None:
    diff = (
        "diff --git a/o b/n\nrename from o\nrename to n\n--- a/zzz\n+++ b/n\n@@ -1 +1 @@\n-a\n+b\n"
    )
    ambiguous(diff)


def test_rename_path_is_not_prefix_stripped() -> None:
    diff = "diff --git a/a/o b/b/n\nrename from a/o\nrename to b/n\n"
    section = only(diff)
    assert (section.old_path, section.new_path) == ("a/o", "b/n")


def test_mode_only_section_needs_no_paths() -> None:
    diff = "diff --git a/f b/f\nold mode 100644\nnew mode 100755\n"
    section = only(diff)
    assert section.kind is SectionKind.MODE_ONLY
    assert (section.old_path, section.new_path, section.hunks) == (None, None, ())


def test_bare_diff_header_is_mode_only() -> None:
    assert only("diff --git a/f b/f\n").kind is SectionKind.MODE_ONLY


def test_old_mode_without_new_mode_is_ambiguous() -> None:
    ambiguous("diff --git a/f b/f\nold mode 100644\n")


def test_binary_section_reads_path_from_binary_line() -> None:
    diff = (
        "diff --git a/bin.dat b/bin.dat\nindex c86..566 100644\n"
        "Binary files a/bin.dat and b/bin.dat differ\n"
    )
    section = only(diff)
    assert section.kind is SectionKind.BINARY
    assert (section.old_path, section.new_path) == ("bin.dat", "bin.dat")


def test_binary_added_and_deleted() -> None:
    added = "diff --git a/b b/b\nnew file mode 100644\nBinary files /dev/null and b/b differ\n"
    deleted = (
        "diff --git a/b b/b\ndeleted file mode 100644\nBinary files a/b and /dev/null differ\n"
    )
    assert (only(added).old_path, only(added).new_path) == (None, "b")
    assert (only(deleted).old_path, only(deleted).new_path) == ("b", None)
    assert only(added).kind is SectionKind.BINARY and only(deleted).kind is SectionKind.BINARY


def test_binary_path_containing_and_and_b_slash_is_read_by_equal_halves() -> None:
    path = "x and b/x"
    diff = f"diff --git a/{path} b/{path}\nBinary files a/{path} and b/{path} differ\n"
    section = only(diff)
    assert (section.old_path, section.new_path) == (path, path)


def test_binary_with_unequal_halves_and_no_rename_lines_is_ambiguous() -> None:
    ambiguous("diff --git a/p b/q\nBinary files a/p and b/q differ\n")


def test_binary_rename_takes_paths_from_rename_lines() -> None:
    diff = "diff --git a/p b/q\nrename from p\nrename to q\nBinary files a/p and b/q differ\n"
    section = only(diff)
    assert section.kind is SectionKind.BINARY
    assert (section.old_path, section.new_path) == ("p", "q")


def test_quoted_binary_paths() -> None:
    diff = 'diff --git x y\nBinary files "a/s p/\\303\\251" and "b/s p/\\303\\251" differ\n'
    assert only(diff).old_path == "s p/é"


def test_git_binary_patch_is_ambiguous() -> None:
    ambiguous("diff --git a/b b/b\nGIT binary patch\nliteral 1\nAc\n\n")


def test_binary_line_with_hunks_is_ambiguous() -> None:
    ambiguous(
        "diff --git a/b b/b\nBinary files a/b and b/b differ\n"
        "--- a/b\n+++ b/b\n@@ -1 +1 @@\n-a\n+b\n"
    )


def test_deleted_headers_without_readable_path_is_ambiguous() -> None:
    ambiguous("diff --git a/e b/e\ndeleted file mode 100644\nindex e69de29..0000000\n")


def test_added_headers_without_paths_is_accepted() -> None:
    section = only("diff --git a/e b/e\nnew file mode 100644\nindex 0000000..e69de29\n")
    assert section.kind is SectionKind.ADDED
    assert (section.old_path, section.new_path) == (None, None)


def test_dev_null_without_matching_mode_header_is_ambiguous() -> None:
    ambiguous("diff --git a/n b/n\n--- /dev/null\n+++ b/n\n@@ -0,0 +1 @@\n+x\n")
    ambiguous("diff --git a/n b/n\n--- a/n\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n")


def test_new_file_mode_with_non_null_old_path_is_ambiguous() -> None:
    ambiguous("diff --git a/n b/n\nnew file mode 100644\n--- a/n\n+++ b/n\n@@ -1 +1 @@\n-a\n+b\n")


def test_changed_section_with_different_paths_and_no_rename_is_ambiguous() -> None:
    ambiguous(text_diff("a/p", "b/q"))


def test_unknown_header_line_is_ambiguous() -> None:
    ambiguous("diff --git a/f b/f\ncopy from f\ncopy to g\n")
    ambiguous("diff --git a/f b/f\nsurprise\n")


def test_duplicate_header_lines_are_ambiguous() -> None:
    ambiguous("diff --git a/f b/f\nnew file mode 100644\nnew file mode 100644\n")


def test_text_before_first_section_is_ambiguous() -> None:
    ambiguous("junk\n" + text_diff("a/f", "b/f"))
    ambiguous("\n")


def test_hunk_without_path_lines_is_ambiguous() -> None:
    ambiguous("diff --git a/f b/f\n@@ -1 +1 @@\n-a\n+b\n")


def test_section_split_needs_space_after_diff_git() -> None:
    ambiguous("diff --gitx a/f b/f\n")


# --- duplicates -----------------------------------------------------------------------------


def test_two_sections_naming_one_path_are_ambiguous() -> None:
    ambiguous(text_diff("a/f", "b/f") + text_diff("a/f", "b/f"))


def test_path_named_in_two_roles_is_ambiguous() -> None:
    rename = "diff --git a/f b/g\nrename from f\nrename to g\n"
    ambiguous(rename + text_diff("a/g", "b/g"))


def test_distinct_paths_are_fine() -> None:
    parsed = parse_diff(text_diff("a/f", "b/f") + text_diff("a/g", "b/g"))
    assert [s.old_path for s in parsed.sections] == ["f", "g"]


def test_by_old_path_lookup() -> None:
    parsed = parse_diff(
        text_diff("a/f", "b/f") + "diff --git a/o b/n\nrename from o\nrename to n\n"
    )
    table = parsed.by_old_path()
    assert set(table) == {"f", "o"}
    assert table["o"].new_path == "n"


# --- path table -----------------------------------------------------------------------------

PATH_TABLE = [
    pytest.param("a/plain.txt", "plain.txt", False, id="plain"),
    pytest.param("a/with space.txt", "with space.txt", True, id="space-tab"),
    pytest.param("a/with space.txt", "with space.txt", False, id="space-no-tab"),
    pytest.param("a/x b/y", "x b/y", True, id="space-then-b-slash"),
    pytest.param("a/a/b", "a/b", False, id="nested-a-prefix"),
    pytest.param('"a/q\\"uote"', 'q"uote', False, id="quoted-quote"),
    pytest.param('"a/back\\\\slash"', "back\\slash", False, id="quoted-backslash"),
    pytest.param('"a/new\\nline"', "new\nline", False, id="quoted-newline"),
    pytest.param('"a/tab\\there"', "tab\there", False, id="quoted-tab"),
    pytest.param('"a/s p/\\303\\251.txt"', "s p/é.txt", True, id="octal-utf8-space"),
    pytest.param('"a/\\342\\202\\254"', "€", False, id="octal-euro"),
    pytest.param('"a/\\377bad"', "�bad", False, id="octal-invalid-utf8"),
    pytest.param("a/<U+202E>mark.txt", "<U+202E>mark.txt", False, id="sanitizing-marker"),
    pytest.param("a/<U+000A>x", "<U+000A>x", False, id="newline-marker-literal"),
    pytest.param("a/é/日本語.txt", "é/日本語.txt", False, id="raw-non-ascii"),
    pytest.param("a/a b/c d.txt", "a b/c d.txt", True, id="two-spaces"),
    pytest.param("a/trailing ", "trailing ", True, id="trailing-space"),
    pytest.param("a/-- a", "-- a", False, id="dashes"),
]


@pytest.mark.parametrize(("token", "expected", "tab"), PATH_TABLE)
def test_path_table(token: str, expected: str, tab: bool) -> None:
    new_token = (
        token.replace("a/", "b/", 1) if token.startswith("a/") else token.replace('"a/', '"b/', 1)
    )
    section = only(text_diff(token, new_token, tab=tab))
    assert section.old_path == expected
    assert section.new_path == expected


def test_only_one_trailing_tab_is_removed() -> None:
    assert only(text_diff("a/x", "b/x", tab=True)).old_path == "x"
    # a path that ends in a raw tab cannot come from git: it is quoted, so this is unreadable
    ambiguous(text_diff("a/x\t", "b/x\t", tab=True))


@pytest.mark.parametrize(
    "token",
    [
        "",  # empty path
        "a/",  # prefix only
        "x/file",  # wrong prefix
        "b/file",  # new-side prefix on the old line
        '"a/unterminated',
        'a/raw"quote',
        "a/raw\\backslash",
        '"a/bad\\qescape"',
        '"a/short\\30"',
        '"a/big\\777"',
        '"a/nul\\000"',
        '"a/raw\ttab"',
        "a/ctl\x01",
        "a/cr\r",
        '"a/x"trail',
        "/dev/null/x",
    ],
)
def test_unreadable_old_paths_are_ambiguous(token: str) -> None:
    ambiguous(text_diff(token, "b/ok"))


def test_unreadable_new_path_is_ambiguous() -> None:
    ambiguous(text_diff("a/ok", "a/ok"))
    ambiguous(text_diff("a/ok", "b/"))


def test_plus_line_must_directly_follow_minus_line() -> None:
    ambiguous("diff --git a/f b/f\n--- a/f\nindex 1..2\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n")
    ambiguous("diff --git a/f b/f\n--- a/f\n")


def test_quoted_dev_null_is_not_dev_null() -> None:
    ambiguous(
        'diff --git a/f b/f\nnew file mode 100644\n--- "/dev/null"\n+++ b/f\n@@ -0,0 +1 @@\n+x\n'
    )


def test_header_line_cannot_forge_path() -> None:
    # the diff --git line is never read, whatever it claims
    diff = "diff --git a/evil b/evil\n--- a/real\n+++ b/real\n@@ -1 +1 @@\n-a\n+b\n"
    assert only(diff).old_path == "real"


# --- hunks ----------------------------------------------------------------------------------


def test_omitted_counts_default_to_one() -> None:
    (hunk,) = only(text_diff("a/f", "b/f", "@@ -10 +10 @@\n-a\n+b\n")).hunks
    assert (hunk.old_count, hunk.new_count) == (1, 1)


def test_zero_count_forms() -> None:
    diff = text_diff("a/f", "b/f", "@@ -0,0 +1,2 @@\n+x\n+y\n@@ -5,2 +6,0 @@\n-p\n-q\n")
    first, second = only(diff).hunks
    assert (first.old_start, first.old_count, first.new_start, first.new_count) == (0, 0, 1, 2)
    assert (second.old_start, second.old_count, second.new_start, second.new_count) == (5, 2, 6, 0)


def test_hunk_heading_after_header_is_ignored() -> None:
    (hunk,) = only(text_diff("a/f", "b/f", "@@ -1 +1 @@ def fn(a, b):\n-a\n+b\n")).hunks
    assert hunk.lines == (HunkLine("-", "a"), HunkLine("+", "b"))


def test_no_newline_marker_is_not_a_line() -> None:
    body = "@@ -1 +1,2 @@\n-a\n\\ No newline at end of file\n+a\n+b\n\\ No newline at end of file\n"
    (hunk,) = only(text_diff("a/f", "b/f", body)).hunks
    assert hunk.lines == (HunkLine("-", "a"), HunkLine("+", "a"), HunkLine("+", "b"))


def test_marker_mid_hunk_and_between_sections() -> None:
    body = "@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n"
    diff = text_diff("a/f", "b/f", body) + text_diff("a/g", "b/g")
    assert len(parse_diff(diff).sections) == 2


def test_marker_first_in_hunk_or_doubled_is_ambiguous() -> None:
    ambiguous(text_diff("a/f", "b/f", "@@ -1 +1 @@\n\\ No newline at end of file\n-a\n+b\n"))
    ambiguous(
        text_diff(
            "a/f", "b/f", "@@ -1 +1 @@\n-a\n+b\n\\ No newline\n\\ No newline at end of file\n"
        )
    )


def test_cr_before_lf_is_content() -> None:
    body = "@@ -1,2 +1,2 @@\n-line05\r\n+EDIT05\r\n line06\r\n"
    # header lines are LF only; a CR on content lines stays
    diff = text_diff("a/f", "b/f", body)
    (hunk,) = only(diff).hunks
    assert hunk.lines == (
        HunkLine("-", "line05\r"),
        HunkLine("+", "EDIT05\r"),
        HunkLine(" ", "line06\r"),
    )


def test_cr_on_a_header_line_makes_the_path_unreadable() -> None:
    ambiguous("diff --git a/f b/f\r\n--- a/f\r\n+++ b/f\r\n@@ -1 +1 @@\r\n-a\r\n+b\r\n")


def test_content_that_looks_like_headers_is_consumed_by_count() -> None:
    body = "@@ -1,2 +1,0 @@\n--- a\n-++ b\n"
    (hunk,) = only(text_diff("a/f", "b/f", body.replace("+1,0", "+0,0"))).hunks
    assert hunk.lines == (HunkLine("-", "-- a"), HunkLine("-", "++ b"))


def test_content_that_looks_like_hunk_header_and_diff_line() -> None:
    body = "@@ -1,2 +1,2 @@\n-@@ -9 +9 @@\n+diff --git a/z b/z\n context\n"
    (hunk,) = only(text_diff("a/f", "b/f", body.replace("-1,2 +1,2", "-1,2 +1,2"))).hunks
    assert hunk.lines[0] == HunkLine("-", "@@ -9 +9 @@")
    assert hunk.lines[1] == HunkLine("+", "diff --git a/z b/z")


def test_unicode_line_separators_do_not_split_lines() -> None:
    body = "@@ -1 +1 @@\n-a b\x0bc\x0cd\x85e\n+f\n"
    (hunk,) = only(text_diff("a/f", "b/f", body)).hunks
    assert hunk.lines[0] == HunkLine("-", "a b\x0bc\x0cd\x85e")


def test_bare_empty_line_in_hunk_is_context() -> None:
    body = "@@ -1,3 +1,3 @@\n a\n\n-c\n+d\n"
    (hunk,) = only(text_diff("a/f", "b/f", body)).hunks
    assert hunk.lines[1] == HunkLine(" ", "")


def test_multiple_hunks_in_order() -> None:
    body = "@@ -1 +1 @@\n-a\n+b\n@@ -5,0 +6 @@\n+z\n@@ -9 +11,0 @@\n-q\n"
    assert len(only(text_diff("a/f", "b/f", body)).hunks) == 3


@pytest.mark.parametrize(
    "body",
    [
        "@@ -1 +1 @@\n-a\n",  # truncated
        "@@ -1,3 +1,3 @@\n a\n-b\n+c\n",  # truncated inside
        "@@ -1 +1 @@\n-a\n+b\n+extra\n",  # more lines than counts
        "@@ -1 +1 @@\n-a\n-b\n+c\n",  # overflow on old side
        "@@ -1 +1 @@\n a\n+b\n",  # context makes new side overflow
        "@@ -1 +1 @@\n-a\n+b\n?odd\n",  # unknown tag after the hunk
        "@@ -1 +1 @@\n-a\n?odd\n+b\n",  # unknown tag inside
        "@@ -0,0 +0,0 @@\n",  # empty hunk
        "@@ -0 +1 @@\n+a\n",  # count 1 at start 0
        "@@ -a +1 @@\n-a\n+b\n",  # malformed
        "@@ -1 +1\n-a\n+b\n",  # missing closing
        "@@ -1 +1 @@x\n-a\n+b\n",  # no space after closing @@
        "@@ -1,2 +1,2 @@\n-a\n-b\n+c\n+d\n@@ -2 +9 @@\n-a\n+b\n",  # second hunk overlaps old side
        "@@ -5 +5 @@\n-a\n+b\n@@ -2 +9 @@\n-a\n+b\n",  # second hunk before the first
        "@@ -1 +5 @@\n-a\n+b\n@@ -3 +2 @@\n-a\n+b\n",  # second hunk before on new side
        "@@ -١ +1 @@\n-a\n+b\n",  # non-ASCII digit
        "@@ -1234567890 +1 @@\n-a\n+b\n",  # absurd number
        "@@ -99999999,99999999 +1 @@\n-a\n+b\n",  # claims more lines than exist
        "@@ -1 +1 @@\n-a\n+b\n\n",  # blank line after the hunk
    ],
)
def test_malformed_hunks_are_ambiguous(body: str) -> None:
    ambiguous(text_diff("a/f", "b/f", body))


def test_added_file_hunk_must_start_at_zero() -> None:
    ambiguous(
        "diff --git a/n b/n\nnew file mode 100644\n--- /dev/null\n+++ b/n\n@@ -1 +1 @@\n-a\n+b\n"
    )


def test_deleted_file_hunk_must_end_at_zero() -> None:
    ambiguous(
        "diff --git a/n b/n\ndeleted file mode 100644\n"
        "--- a/n\n+++ /dev/null\n@@ -1 +1 @@\n-a\n+b\n"
    )


def test_ambiguity_error_names_line_and_reason_without_echoing_content() -> None:
    err = ambiguous(text_diff("a/f", "b/f", "@@ -1 +1 @@\n-secret\n"))
    assert err.reason == "truncated_hunk"
    assert "secret" not in str(err)
    assert err.line > 0


def test_huge_hunk_parses_quickly() -> None:
    n = 300_000
    body = f"@@ -1,{n} +1,{n} @@\n" + "".join(f" line{i}\n" for i in range(n))
    started = time.monotonic()
    (hunk,) = only(text_diff("a/f", "b/f", body)).hunks
    assert len(hunk.lines) == n
    assert time.monotonic() - started < 5


def test_many_sections_parse() -> None:
    diff = "".join(text_diff(f"a/f{i}", f"b/f{i}") for i in range(2000))
    assert len(parse_diff(diff).sections) == 2000


def test_no_trailing_newline_after_last_line() -> None:
    (hunk,) = only(text_diff("a/f", "b/f").rstrip("\n")).hunks
    assert hunk.lines[-1] == HunkLine("+", "b")


# --- spike A fixtures -------------------------------------------------------------------------


def spike_diffs() -> list[Path]:
    return sorted(SPIKE_A.glob("*/*.diff"))


def test_spike_fixtures_are_present() -> None:
    names = {p.parent.name for p in spike_diffs()}
    assert len(names) == 23
    assert len(spike_diffs()) >= 22 * 4


@pytest.mark.parametrize("path", spike_diffs(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_spike_a_diffs_parse(path: Path) -> None:
    parsed = parse_diff(path.read_bytes().decode("utf-8"))
    for section in parsed.sections:
        for hunk in section.hunks:
            assert sum(1 for ln in hunk.lines if ln.tag in " -") == hunk.old_count
            assert sum(1 for ln in hunk.lines if ln.tag in " +") == hunk.new_count


@pytest.mark.parametrize(
    "case_dir",
    sorted(p for p in SPIKE_A.iterdir() if (p / "old_lengths.json").is_file()),
    ids=lambda p: p.name,
)
def test_spike_a_hunks_fit_the_old_files_at_every_context_size(case_dir: Path) -> None:
    lengths = json.loads((case_dir / "old_lengths.json").read_text())
    seen = 0
    for size in ("U0", "U1", "U3", "U10"):
        parsed = parse_diff((case_dir / f"{size}.diff").read_bytes().decode("utf-8"))
        for section in parsed.sections:
            if section.old_path in lengths:
                for hunk in section.hunks:
                    end = hunk.old_start + hunk.old_count - 1
                    assert end <= lengths[section.old_path]
        seen += 1
    assert seen == 4


def kinds(case: str) -> list[SectionKind]:
    text = (SPIKE_A / case / "U3.diff").read_bytes().decode("utf-8")
    return [s.kind for s in parse_diff(text).sections]


def test_spike_a_case_kinds() -> None:
    assert kinds("14-file-deleted")[0] is SectionKind.DELETED
    assert kinds("15-rename-with-edit") == [SectionKind.RENAMED]
    assert kinds("16-rename-no-detection") == [SectionKind.DELETED, SectionKind.ADDED]
    assert kinds("19-binary-change")[0] is SectionKind.BINARY
    assert kinds("20-mode-only") == [SectionKind.MODE_ONLY]
    assert kinds("22-crlf-to-lf") == [SectionKind.CHANGED]


def test_spike_a_quoted_path_and_crlf_content() -> None:
    text = (SPIKE_A / "21-path-with-space-and-utf8" / "U3.diff").read_bytes().decode("utf-8")
    assert only(text).new_path == "s p/é.txt"
    crlf = (SPIKE_A / "17-crlf-edit" / "U0.diff").read_bytes().decode("utf-8")
    assert only(crlf).hunks[0].lines[0].text.endswith("\r")


def test_spike_a_header_lookalike_removed_lines() -> None:
    text = (SPIKE_A / "18-removed-line-looks-like-header" / "U0.diff").read_bytes().decode()
    texts = [ln.text for h in only(text).hunks for ln in h.lines]
    assert "-- a" in texts
