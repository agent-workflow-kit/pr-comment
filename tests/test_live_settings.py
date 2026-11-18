"""The live suite's own settings and pure helpers (issue #39): what it refuses, what it never
prints, how it reads a diff, a marker and rendered HTML. No host, no network."""

from __future__ import annotations

import re

import pytest

from tests.live import helpers
from tests.live.settings import (
    OPT_IN,
    from_env,
    redact,
    repo_of_ref,
)

REPO = "github.com/acme/widgets"
LAB = "gitlab.com/acme/widgets"


def env(**extra: str) -> dict[str, str]:
    base = {
        "PR_COMMENT_LIVE": "1",
        "PR_COMMENT_LIVE_MODE": "run",
        "PR_COMMENT_LIVE_ALLOW": f"{REPO},{LAB}",
        "PR_COMMENT_LIVE_REPORT_DIR": "/scratch/report",
        "PR_COMMENT_LIVE_GITHUB_PR": f"{REPO}#7",
    }
    base.update(extra)
    return base


# --- the opt-in and the refusals -----------------------------------------------------------------


def test_nothing_is_allowed_without_the_opt_in() -> None:
    settings = from_env({})
    assert not settings.opted_in
    reason = settings.skip_reason()
    assert reason is not None and OPT_IN in reason


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_the_value_one_opts_in(value: str) -> None:
    assert not from_env({OPT_IN: value}).opted_in


def test_the_default_mode_is_plan_and_needs_no_target_or_allowlist() -> None:
    settings = from_env({OPT_IN: "1"})
    assert settings.mode == "plan" and not settings.writes
    assert settings.refusals() == []


@pytest.mark.parametrize("mode", ["preflight", "run"])
def test_beyond_plan_mode_an_allowlist_and_a_target_are_required(mode: str) -> None:
    refusals = from_env({OPT_IN: "1", "PR_COMMENT_LIVE_MODE": mode}).refusals()
    assert any("PR_COMMENT_LIVE_ALLOW" in r for r in refusals)
    assert any("no target" in r for r in refusals)


def test_run_mode_needs_a_report_directory_for_the_ledger() -> None:
    settings = from_env(env(PR_COMMENT_LIVE_REPORT_DIR=""))
    assert any("PR_COMMENT_LIVE_REPORT_DIR" in r for r in settings.refusals())


def test_a_target_outside_the_allowlist_is_refused_without_naming_it() -> None:
    settings = from_env(env(PR_COMMENT_LIVE_GITHUB_PR="github.com/somebody-else/private-repo#3"))
    problems = " ".join(settings.refusals())
    assert "PR_COMMENT_LIVE_GITHUB_PR" in problems
    assert "somebody-else" not in problems and "private-repo" not in problems


def test_a_target_that_names_no_repo_is_refused() -> None:
    settings = from_env(env(PR_COMMENT_LIVE_GITHUB_PR="not a reference"))
    assert any("PR_COMMENT_LIVE_GITHUB_PR" in r for r in settings.refusals())


@pytest.mark.parametrize("entry", ["*", "github.com/acme/*", "acme", "github.com", "a b/c"])
def test_an_allowlist_entry_must_be_an_exact_repo_id(entry: str) -> None:
    settings = from_env(env(PR_COMMENT_LIVE_ALLOW=entry))
    assert any("PR_COMMENT_LIVE_ALLOW" in r for r in settings.refusals())
    assert settings.allow == ()


def test_the_enterprise_key_needs_another_host_and_the_plain_key_a_shared_one() -> None:
    wrong = from_env(env(PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR=f"{REPO}#1"))
    assert any("ENTERPRISE" in r for r in wrong.refusals())
    swapped = from_env(
        env(
            PR_COMMENT_LIVE_ALLOW="ghe.example.com/acme/widgets",
            PR_COMMENT_LIVE_GITHUB_PR="ghe.example.com/acme/widgets#1",
        )
    )
    assert any("PR_COMMENT_LIVE_GITHUB_PR names a host" in r for r in swapped.refusals())
    fine = from_env(
        env(
            PR_COMMENT_LIVE_ALLOW=f"{REPO},ghe.example.com/acme/widgets",
            PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR="ghe.example.com/acme/widgets#1",
        )
    )
    assert fine.refusals() == [] and fine.targets["GITHUB_ENTERPRISE"].is_enterprise


def test_an_old_head_must_be_forty_hex_characters() -> None:
    bad = from_env(env(PR_COMMENT_LIVE_GITHUB_OLD_HEAD="abc"))
    assert any("OLD_HEAD" in r for r in bad.refusals())
    good = from_env(env(PR_COMMENT_LIVE_GITHUB_OLD_HEAD="A" * 40))
    assert good.targets["GITHUB"].old_head == "a" * 40


def test_a_report_directory_and_cleanup_command_must_be_absolute() -> None:
    settings = from_env(env(PR_COMMENT_LIVE_REPORT_DIR="rel", PR_COMMENT_LIVE_CLEANUP_CMD="rm"))
    problems = settings.refusals()
    assert any("REPORT_DIR" in r for r in problems) and any("CLEANUP_CMD" in r for r in problems)


def test_a_bad_mode_and_a_bad_run_id_are_refused() -> None:
    assert any("MODE" in p for p in from_env({OPT_IN: "1", "PR_COMMENT_LIVE_MODE": "go"}).problems)
    assert any(
        "RUN_ID" in p for p in from_env({OPT_IN: "1", "PR_COMMENT_LIVE_RUN_ID": "A_B"}).problems
    )


def test_a_run_id_is_kept_and_a_generated_one_has_the_same_shape() -> None:
    assert from_env({"PR_COMMENT_LIVE_RUN_ID": "abc123"}).tag == "live-abc123"
    generated = from_env({}).run_id
    assert re.fullmatch(r"[a-z0-9]{6,24}", generated)


def test_no_problem_ever_contains_a_value_of_the_environment() -> None:
    secret = "tok-0123456789"
    settings = from_env(
        env(
            PR_COMMENT_LIVE_GITHUB_PR=f"github.com/acme/{secret}#9",
            PR_COMMENT_LIVE_REPORT_DIR=secret,
            PR_COMMENT_LIVE_MODE=secret,
            PR_COMMENT_LIVE_RUN_ID=secret,
            PR_COMMENT_LIVE_CLEANUP_CMD=secret,
            PR_COMMENT_LIVE_FS_DIR=secret,
            PR_COMMENT_LIVE_GITHUB_OLD_HEAD=secret,
        )
    )
    assert settings.problems
    assert secret not in " ".join(settings.refusals())


def test_allowed_is_exact_and_case_insensitive() -> None:
    settings = from_env(env())
    assert settings.target_allowed("GitHub.com/Acme/Widgets")
    assert not settings.target_allowed("github.com/acme/widgets-2")
    assert not settings.target_allowed(None)


# --- references ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ref", "repo"),
    [
        ("github.com/Acme/Widgets#7", "github.com/acme/widgets"),
        ("https://github.com/acme/widgets/pull/7", "github.com/acme/widgets"),
        (
            "https://gitlab.com/acme/group/widgets/-/merge_requests/7",
            "gitlab.com/acme/group/widgets",
        ),
        ("gitlab.example.com:8443/a/b#3", "gitlab.example.com:8443/a/b"),
        ("widgets#7", None),
        ("github.com/acme/widgets", None),
        ("-x#1", None),
        ("", None),
    ],
)
def test_the_repo_a_reference_names(ref: str, repo: str | None) -> None:
    assert repo_of_ref(ref) == repo


# --- redaction -----------------------------------------------------------------------------------


def test_redact_replaces_the_values_of_secret_looking_variables_by_their_names() -> None:
    environment = {
        "GH_TOKEN": "ghp_abcdef123456",
        "PATH": "/usr/bin",
        "SLACK_WEBHOOK_URL": "https://hooks.example.com/services/XYZ",
        "SHORT_KEY": "ab",
    }
    text = "boom ghp_abcdef123456 at https://hooks.example.com/services/XYZ /usr/bin ab"
    out = redact(text, environment)
    assert "ghp_abcdef123456" not in out and "hooks.example.com" not in out
    assert "<GH_TOKEN>" in out and "<SLACK_WEBHOOK_URL>" in out
    assert "/usr/bin" in out  # not a secret
    assert out.endswith(" ab")  # too short to be worth replacing


# --- helpers -------------------------------------------------------------------------------------

DIFF = """diff --git a/src/a.py b/src/a.py
index 111..222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,5 @@
 keep
-gone
+added one
+added two
 tail
@@ -20,2 +22,3 @@ def f():
 ctx
+added three
 ctx2
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+first
+second
\\ No newline at end of file
diff --git a/old.bin b/old.bin
Binary files a/old.bin and b/old.bin differ
"""


def test_added_lines_follow_the_new_side_of_every_hunk() -> None:
    assert helpers.added_lines(DIFF) == [
        ("src/a.py", 2),
        ("src/a.py", 3),
        ("src/a.py", 23),
        ("new.txt", 1),
        ("new.txt", 2),
    ]


def test_a_plus_plus_plus_line_inside_a_hunk_is_content_not_a_header() -> None:
    diff = "--- a/x\n+++ b/x\n@@ -1,1 +1,2 @@\n a\n+++ not a header\n"
    assert helpers.added_lines(diff) == [("x", 2)]


def test_added_lines_of_nothing_is_nothing() -> None:
    assert helpers.added_lines("") == []


def test_pick_lines_cycles_and_handles_empty() -> None:
    lines = [("a", 1), ("a", 2)]
    assert helpers.pick_lines(lines, 5) == [("a", 1), ("a", 2), ("a", 1), ("a", 2), ("a", 1)]
    assert helpers.pick_lines([], 3) == []


def test_draft_ids_are_fixed_by_the_run_and_the_label_and_match_the_draft_grammar() -> None:
    pattern = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
    a = helpers.draft_id("run1", "x")
    assert pattern.match(a)
    assert a == helpers.draft_id("run1", "x")
    assert a != helpers.draft_id("run1", "y") and a != helpers.draft_id("run2", "x")


MARKER = helpers.MARKER


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (f"text\n\n{MARKER}", True),
        (f"text\n\n{MARKER}\n", True),
        (f"text\n\n{MARKER}   \n\n", True),
        (f"text\n\n{MARKER}\n<!-- forgectl:dedupe:prc:comment:abc -->", True),
        (f"text\n\n{MARKER}\n<!-- forgectl:dedupe:prc:comment:abc -->\n\n", True),
        (f"{MARKER}\ntext", False),
        (f"> {MARKER}", False),
        (f"text\n{MARKER}\n<!-- forgectl:dedupe:a -->\n<!-- forgectl:dedupe:b -->", False),
        (f"text\n{MARKER} x", False),
        ("", False),
        # Python strips Unicode whitespace; forgectl: row ts.unicode_whitespace_marker
        (f"{MARKER}\n\u00a0", True),
    ],
)
def test_marker_detection(body: str, expected: bool) -> None:
    assert helpers.has_marker(body) is expected


def test_tagged_comments_and_flattening() -> None:
    doc = {
        "threads": [
            {"id": "t1", "comments": [{"id": "c1", "body": "a live-x"}, {"id": "c2", "body": "b"}]},
            {"id": "t2", "comments": [{"id": "c3", "body": "live-x too"}]},
        ]
    }
    flat = helpers.flatten_comments(doc)
    assert [c["id"] for c in flat] == ["c1", "c2", "c3"]
    assert flat[0]["_thread"]["id"] == "t1"
    assert [c["id"] for c in helpers.tagged(flat, "live-x")] == ["c1", "c3"]
    assert helpers.flatten_comments(None) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "--json", "--timeout=60s", "--dedupe-key=k", "--yes", "--body-file", "-"],
        ["review", "--json", "--file", "-", "--dry-run", "--decision-id=d"],
        ["show", "--json", "gh/x#1"],
    ],
)
def test_documented_flag_forms_pass(argv: list[str]) -> None:
    assert helpers.flag_form_problems(argv) == []


@pytest.mark.parametrize(
    "argv",
    [
        ["comment", "--dedupe-key", "k"],
        ["comment", "--body-file", "x.txt"],
        ["comment", "--file"],
        ["comment", "--yes=1"],
        ["comment", "--actor", "auto:x"],
    ],
)
def test_other_flag_forms_are_reported(argv: list[str]) -> None:
    assert helpers.flag_form_problems(argv)


def test_html_inspection() -> None:
    html = helpers.Html(
        '<p>hi <a href="https://example.com/octocat" class="user-mention">@octocat</a> '
        '<a href="javascript:alert(1)">x</a></p><pre class="x y"><code>c</code></pre>'
        '<img src="u"><a data-user="42" href="/other">o</a>'
    )
    assert html.count("pre") == 1 and html.count("img") == 1
    assert [a["href"] for a in html.links_to("octocat")] == ["https://example.com/octocat"]
    assert html.links_to("nobody") == []
    assert html.hrefs_with_scheme("javascript:") == ["javascript:alert(1)"]
    assert "hi" in html.text and "<" not in html.text
    assert helpers.Html("<pre class='b a'></pre>").shape() == "pre.a b"
    assert helpers.Html("<pre><code></code></pre>").shape() != helpers.Html("<pre></pre>").shape()
    assert helpers.distinct_shapes("a", "b") and not helpers.distinct_shapes("a", "a")
