"""The posting plan (``src/pr_comment/plan.py``): what a plan holds, that it is deterministic and
that the hash covers everything that will be posted (docs/interface.md: Posting plan)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pr_comment import keys
from pr_comment.assemble import MARKER
from pr_comment.config import ConfigError
from pr_comment.diffmap import DiffMapper
from pr_comment.plan import (
    MAX_INLINE_COMMENTS,
    Plan,
    TooManyInlineComments,
    build_review_plan,
    build_text_plan,
    marker_present,
    role_of,
    with_dedupe_line,
)
from tests.posting import (
    BASE_SHA,
    DRAFT_ID,
    MB,
    A,
    B,
    context,
    draft_doc,
    finding,
    settings,
    show_of,
    source_of,
)


def review(
    findings: list[dict[str, Any]],
    *,
    extra: dict[str, str] | None = None,
    approve: bool = False,
    event: str = "comment",
    body: str = "summary",
    **ctx: Any,
) -> Plan:
    source, _ = source_of(draft_doc(findings, event=event, body=body), approve=approve)
    return build_review_plan(context(**ctx), source, extra)


def hash_of(plan: Plan) -> str:
    return keys.plan_sha256(
        pr=plan.pr,
        action=plan.action,
        event=plan.event,
        head_sha=plan.head_sha,
        base_ref=plan.base_ref,
        dedupe_key=plan.dedupe_key,
        to=plan.to,
        actor=plan.actor,
        decision_ids=plan.decision_ids,
        bodies=[{"role": b.role, "text": b.text} for b in plan.bodies],
        inline_comments=[
            {
                "finding_id": c.finding_id,
                "path": c.path,
                "line": c.line,
                "start_line": c.start_line,
                "side": c.side,
                "role": c.role,
            }
            for c in plan.inline_comments
        ],
        follow_ups=[{"action": f.action, "thread_id": f.thread_id} for f in plan.follow_ups],
    )


# --- a review --------------------------------------------------------------------------------


def test_review_plan_holds_bodies_inline_comments_and_the_key() -> None:
    plan = review([finding("f1", "a.py", 3), finding("f2", "b.py", None), finding("f3", "c.py", 9)])
    assert [b.role for b in plan.bodies] == ["body", "inline:f1", "inline:f3"]
    assert [c.finding_id for c in plan.inline_comments] == ["f1", "f3"]
    assert plan.file_level == ("f2",)
    assert plan.posted_inline == ("f1", "f3")
    assert plan.dedupe_key == f"prc:rv:{DRAFT_ID}"
    assert (plan.pr, plan.head_sha, plan.base_ref, plan.event) == (
        "github.com/acme/widgets#7",
        A,
        "main",
        "comment",
    )
    assert marker_present(plan)
    body = plan.bodies[0].text
    assert body.startswith("Drafted with AI assistance")
    assert "### File-level findings" in body
    assert plan.bodies[1].text.endswith(MARKER)


def test_the_hash_is_the_hash_of_the_plan_fields_and_stable() -> None:
    first = review([finding("f1")])
    second = review([finding("f1")])
    assert first.plan_sha256 == second.plan_sha256 == hash_of(first)
    assert first.input == second.input
    assert first.command() == second.command()


def test_the_review_input_is_the_plan_byte_for_byte() -> None:
    plan = review([finding("f1", "a.py", 5, start=3, side="right"), finding("f2", "b.py", 2)])
    sent = json.loads(plan.input)
    assert list(sent) == ["event", "body", "comments"]
    assert sent["event"] == "comment"
    assert sent["body"] == plan.bodies[0].text
    assert sent["comments"] == [
        {"path": "a.py", "line": 5, "side": "right", "start_line": 3, "body": plan.bodies[1].text},
        {"path": "b.py", "line": 2, "side": "right", "body": plan.bodies[2].text},
    ]
    assert plan.input.decode("utf-8")  # strict UTF-8 JSON, which forgectl reads


def test_a_non_ascii_review_input_is_utf8_not_escaped() -> None:
    plan = review([finding("f1", body="日本語 \U0001f600")])
    assert "日本語 \U0001f600" in plan.input.decode("utf-8")
    assert b"\\u" not in plan.input
    sent = json.loads(plan.input)
    assert sent["comments"][0]["body"] == plan.bodies[1].text


def test_a_large_cjk_draft_stays_under_forgectls_16_mib_review_file_limit() -> None:
    # 21000 CJK characters are 63000 bytes of UTF-8 (under the 64 KiB cap) but 126000 bytes as
    # \uXXXX escapes: 150 of them are about 9.5 MB and 19 MB, either side of 16 MiB.
    text = "漢" * 21000
    plan = review([finding(f"f{i}", f"p{i}.py", 3, body=text) for i in range(150)])
    assert len(plan.input) < 16 * 1024 * 1024
    assert len(plan.input) > 9_000_000
    assert json.loads(plan.input)["comments"][149]["body"] == plan.bodies[150].text


def test_a_review_approve_has_its_own_key_and_event() -> None:
    plan = review([finding("f1")], approve=True)
    assert plan.event == "approve"
    assert plan.dedupe_key == f"prc:ra:{DRAFT_ID}:{A[:12]}"
    assert json.loads(plan.input)["event"] == "approve"


def test_a_caller_key_replaces_the_computed_key() -> None:
    plan = review([finding("f1")], dedupe_key="my-key")
    assert plan.dedupe_key == "my-key"
    assert "--dedupe-key=my-key" in plan.command()


def test_the_command_is_the_real_call_with_yes_and_the_pinned_values() -> None:
    plan = review([finding("f1")], decision_ids=["d1", "d2"])
    assert plan.command() == [
        "forgectl",
        "review",
        "github.com/acme/widgets#7",
        "--file",
        "-",
        f"--expected-head={A}",
        "--expected-base=main",
        f"--dedupe-key=prc:rv:{DRAFT_ID}",
        "--json",
        "--yes",
        "--actor=human:alice",
        "--decision-id=d1",
        "--decision-id=d2",
    ]
    assert plan.precheck_args() == [
        "github.com/acme/widgets#7",
        "--file",
        "-",
        f"--expected-head={A}",
        "--expected-base=main",
        f"--dedupe-key=prc:rv:{DRAFT_ID}",
        "--dry-run",
        "--actor=human:alice",
        "--decision-id=d1",
        "--decision-id=d2",
    ]


def test_override_escalation_for_forgectl_is_in_the_command_not_the_hash() -> None:
    plain = review([finding("f1")])
    override = review([finding("f1")], override_forgectl=True)
    assert override.command()[-1] == "--override-escalation"
    assert "--override-escalation" not in plain.command()
    assert "--override-escalation" not in override.precheck_args()
    assert plain.plan_sha256 == override.plan_sha256


# --- the hash covers everything that is posted -----------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"actor": "auto:bot"},
        {"decision_ids": ["d1"]},
        {"decision_ids": ["d1", "d2"]},
        {"dedupe_key": "other-key"},
        {"allow_mentions": ["zed"]},
    ],
)
def test_the_hash_changes_with_the_identity_and_the_gates(change: dict[str, Any]) -> None:
    findings = [finding("f1", body="hello @zed")]
    assert review(findings).plan_sha256 != review(findings, **change).plan_sha256


def test_the_hash_changes_with_the_decision_id_order() -> None:
    one = review([finding("f1")], decision_ids=["a", "b"])
    two = review([finding("f1")], decision_ids=["b", "a"])
    assert one.plan_sha256 != two.plan_sha256


def test_the_hash_changes_with_the_config_that_enters_the_text() -> None:
    base = review([finding("f1")])
    from pr_comment.config import Footer

    footer = review(
        [finding("f1")],
        repo_settings=settings(footer=Footer(human="Reviewed by {user} here")),
    )
    assert footer.plan_sha256 != base.plan_sha256


def test_the_hash_changes_with_the_head_and_the_pinned_base() -> None:
    doc = draft_doc([finding("f1")])
    at_a, _ = source_of(doc)
    at_b, _ = source_of(doc, head=B)
    plan_a = build_review_plan(context(), at_a)
    plan_b = build_review_plan(context(show_of(head=B)), at_b)
    assert plan_a.plan_sha256 != plan_b.plan_sha256
    assert plan_b.head_sha == B


def test_a_mention_allow_entry_changes_the_posted_text_and_the_hash() -> None:
    findings = [finding("f1", body="ping @zed")]
    plain = review(findings)
    allowed = review(findings, allow_mentions=["zed"])
    assert "＠zed" in plain.bodies[1].text
    assert "@zed" in allowed.bodies[1].text
    assert plain.plan_sha256 != allowed.plan_sha256


def test_the_hash_covers_the_final_text_after_forgectls_neutralizing() -> None:
    plan = review([finding("f1", body="ok\n/approve now\nforgectl:dedupe:x")])
    inline = plan.bodies[1].text
    assert "\n /approve now\n" in inline
    assert "forgectl:dedupe-x" in inline
    assert plan.neutralized_lines == 0 + 1  # the leading `/` line was already spaced by us
    assert plan.markers_removed == 1
    assert plan.plan_sha256 == hash_of(plan)


def test_the_dialog_text_shows_forgectls_dedupe_line_the_hash_does_not() -> None:
    plan = review([finding("f1")])
    shown = plan.shown_bodies()
    assert shown[0].text == with_dedupe_line(plan.bodies[0].text, plan.dedupe_key)
    assert shown[0].text.endswith(f"{MARKER}\n<!-- forgectl:dedupe:{plan.dedupe_key} -->")
    assert "forgectl:dedupe" not in plan.bodies[0].text


# --- moved findings --------------------------------------------------------------------------


def test_a_left_finding_moves_when_the_merge_base_changed() -> None:
    doc = draft_doc([finding("f1", side="left"), finding("f2")])
    source, _ = source_of(doc, merge_base="2" * 40)
    plan = build_review_plan(context(), source)
    assert [(m.id, m.reason) for m in plan.moved] == [("f1", "base_changed")]
    assert plan.posted_inline == ("f2",)
    assert "### Findings that could not be anchored" in plan.bodies[0].text
    assert f"(at {A[:12]})" in plan.bodies[0].text
    assert plan.counts["moved"]["base_changed"] == 1


def test_this_runs_moves_keep_draft_order_across_reanchoring_and_the_precheck() -> None:
    doc = draft_doc(
        [
            finding("f1", side="left"),
            finding("f2", "b.py", 4),
            finding("f3", "c.py", 8),
            finding("f4", side="left", line=6),
        ]
    )
    source, _ = source_of(doc, merge_base="2" * 40)
    plan = build_review_plan(
        context(), source, {"f3": "invalid_position", "f2": "files_incomplete"}
    )
    assert [(m.id, m.reason) for m in plan.moved] == [
        ("f1", "base_changed"),
        ("f2", "files_incomplete"),
        ("f3", "invalid_position"),
        ("f4", "base_changed"),
    ]
    assert plan.posted_inline == ()
    body = plan.bodies[0].text
    assert body.index("body f1") < body.index("body f2") < body.index("body f3")
    assert [b.role for b in plan.bodies] == ["body"]


def test_a_precheck_move_shows_the_head_it_was_validated_at() -> None:
    doc = draft_doc([finding("f1", "a.py", 7)])
    source, _ = source_of(doc, head=B)
    plan = build_review_plan(context(show_of(head=B)), source, {"f1": "invalid_position"})
    assert f"` a.py:7 ` (at {B[:12]}) — the line is not part of this pull request's diff" in (
        plan.bodies[0].text
    )


def test_a_reanchor_move_shows_the_last_known_anchor_at_a() -> None:
    gone = (
        "diff --git a/a.py b/a.py\ndeleted file mode 100644\nindex 1..0000000\n"
        "--- a/a.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    doc = draft_doc([finding("f1", "a.py", 7, start=5)])
    source, _ = source_of(doc, head=B, mapper=DiffMapper.from_text(gone))
    plan = build_review_plan(context(show_of(head=B)), source)
    assert [(m.id, m.reason) for m in plan.moved] == [("f1", "file_removed")]
    assert f"` a.py:5-7 ` (at {A[:12]})" in plan.bodies[0].text


def test_a_path_with_a_marker_moves_before_the_precheck() -> None:
    plan = review([finding("f1", "x<U+202E>y.py", 3), finding("f2", "a.py", 3)])
    assert [(m.id, m.reason) for m in plan.moved] == [("f1", "invalid_position")]
    assert plan.posted_inline == ("f2",)


def test_unanchored_entries_follow_this_runs_moves_and_are_not_counted_as_moved() -> None:
    old = {"finding": finding("u1", "old.py", 4), "head_sha": "d" * 40, "reason": "changed"}
    doc = draft_doc([finding("f1", side="left")], unanchored=[old])
    source, _ = source_of(doc, merge_base="2" * 40)
    plan = build_review_plan(context(), source)
    body = plan.bodies[0].text
    assert body.index("body f1") < body.index("body u1")
    assert f"(at {'d' * 12})" in body
    assert [m.id for m in plan.moved] == ["f1"]


def test_an_extra_move_must_name_an_inline_finding() -> None:
    doc = draft_doc([finding("f1", line=None)])
    source, _ = source_of(doc)
    with pytest.raises(ValueError):
        build_review_plan(context(), source, {"f1": "invalid_position"})
    with pytest.raises(ValueError):
        build_review_plan(context(), source, {"nope": "invalid_position"})


def test_an_unknown_move_reason_is_refused() -> None:
    doc = draft_doc([finding("f1")])
    source, _ = source_of(doc)
    with pytest.raises(ValueError):
        build_review_plan(context(), source, {"f1": "because"})


# --- the report ------------------------------------------------------------------------------


def test_mentions_are_reported_in_posted_order_with_one_neutralizer() -> None:
    plan = review(
        [finding("f1", body="cc @inline1"), finding("f2", body="cc @Inline2 and @zed")],
        body="hello @amy",
        repo_settings=settings(),
    )
    assert plan.mentions_neutralized == ("amy", "inline1", "inline2", "zed")
    assert plan.counts["mentions_neutralized"] == 4


def test_the_mention_report_starts_with_the_attribution_line() -> None:
    from pr_comment.config import Footer

    plan = review(
        [finding("f1", body="cc @later")],
        repo_settings=settings(footer=Footer(human="Posted by {user} for @first")),
    )
    assert plan.mentions_neutralized == ("first", "later")


def test_degraded_suggestions_are_collected_with_their_reasons() -> None:
    plan = review(
        [
            finding("f1", suggestion={"lines": ["ok = 1"]}),
            finding("f2", "b.py", 4, suggestion={"lines": ["```", "x"]}),
            finding("f3", "c.py", 5, suggestion={"lines": ["/slash"]}),
            finding("f4", "d.py", 6, suggestion={"lines": ["a‮b"]}),
            finding("f5", "e.py", 7, suggestion={"lines": ["forgectl:dedupe:x"]}),
        ]
    )
    assert [(d.id, d.reason) for d in plan.suggestions_degraded] == [
        ("f2", "fence"),
        ("f3", "slash_line"),
        ("f4", "invisible_chars"),
        ("f5", "dedupe_text"),
    ]
    assert plan.counts["suggestions_degraded"] == 4
    assert "```suggestion\nok = 1\n```" in plan.bodies[1].text


def test_a_moved_findings_suggestion_is_not_listed_as_degraded() -> None:
    plan = review([finding("f1", suggestion={"lines": ["x"]})], extra={"f1": "invalid_position"})
    assert plan.suggestions_degraded == ()
    assert "could not be posted inline" in plan.bodies[0].text


def test_counts_are_taken_over_the_raw_parts_and_severities() -> None:
    plan = review(
        [
            finding("f1", severity="major", body="see ![x](http://e.com) <b>x</b>"),
            finding("f2", "b.py", None, severity="nit", title="T"),
        ],
        body="a <i>b</i> https://x.example/y",
    )
    c = plan.counts
    assert c["inline"] == 1 and c["file_level"] == 1
    assert c["severity"] == {"blocker": 0, "major": 1, "minor": 0, "nit": 1, "info": 0}
    assert c["images"] == 1
    assert c["raw_html"] == 4
    assert c["links"] >= 2
    assert set(c["moved"]) == {
        "changed",
        "file_removed",
        "diff_unavailable",
        "base_changed",
        "invalid_position",
        "files_incomplete",
    }


def test_the_posted_bodies_hold_no_raw_html_or_image_syntax() -> None:
    plan = review([finding("f1", body="![x](u) <img src=x>")], body="<script>")
    for b in plan.bodies:
        assert "<img" not in b.text and "<script" not in b.text and "![" not in b.text


# --- follow-ups ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("backend", "event", "setting", "expected"),
    [
        ("gitlab", "comment", "non_blocking", True),
        ("gitlab", "request_changes", "non_blocking", False),
        ("gitlab", "request_changes", "always", True),
        ("gitlab", "comment", "never", False),
        ("github", "comment", "always", False),
    ],
)
def test_the_gitlab_body_thread_follow_up(
    backend: str, event: str, setting: Any, expected: bool
) -> None:
    from pr_comment.config import Gitlab

    pr = "gitlab.com/acme/widgets#7" if backend == "gitlab" else "github.com/acme/widgets#7"
    show = show_of(pr, backend=backend)
    doc = draft_doc([finding("f1")], event=event, pr=pr)
    source, _ = source_of(doc)
    plan = build_review_plan(
        context(show, repo_settings=settings(gitlab=Gitlab(resolve_body_thread=setting))), source
    )
    assert [(f.action, f.thread_id) for f in plan.follow_ups] == (
        [("resolve", None)] if expected else []
    )
    assert plan.document()["follow_ups"] == (
        [{"action": "resolve", "thread_id": None}] if expected else []
    )


def test_a_follow_up_is_part_of_the_hash() -> None:
    from pr_comment.config import Gitlab

    pr = "gitlab.com/acme/widgets#7"
    source, _ = source_of(draft_doc([finding("f1")], pr=pr))
    show = show_of(pr, backend="gitlab")
    never = build_review_plan(
        context(show, repo_settings=settings(gitlab=Gitlab(resolve_body_thread="never"))), source
    )
    yes = build_review_plan(context(show), source)
    assert never.plan_sha256 != yes.plan_sha256


# --- the other actions -----------------------------------------------------------------------


def test_a_comment_plan() -> None:
    plan = build_text_plan(
        context(), action="comment", text="hello\r\nworld", head_sha=A, base_ref="main"
    )
    assert plan.dedupe_key == keys.comment_key("PR_kwDOAAAAAQ", "hello\nworld")
    assert plan.bodies[0].text.endswith("hello\nworld\n\n" + MARKER)
    assert plan.input == plan.bodies[0].text.encode()
    assert plan.event is None and plan.to is None
    assert plan.command()[:5] == [
        "forgectl",
        "comment",
        "github.com/acme/widgets#7",
        "--body-file",
        "-",
    ]
    assert plan.plan_sha256 == hash_of(plan)


def test_a_reply_plan_hashes_the_target() -> None:
    one = build_text_plan(
        context(), action="reply", text="hi", head_sha=A, base_ref="main", to="gh:ic:IC_abc"
    )
    two = build_text_plan(
        context(), action="reply", text="hi", head_sha=A, base_ref="main", to="gh:ic:IC_abd"
    )
    assert one.dedupe_key == keys.reply_key("PR_kwDOAAAAAQ", "gh:ic:IC_abc", "hi")
    assert one.plan_sha256 != two.plan_sha256
    assert "--to=gh:ic:IC_abc" in one.command()
    assert "--to=gh:ic:IC_abc" in one.precheck_args()


def test_an_approve_plan_without_a_body_file_still_carries_the_marker() -> None:
    plan = build_text_plan(context(), action="approve", text=None, head_sha=A, base_ref="main")
    assert plan.event == "approve"
    assert plan.bodies[0].text.endswith(MARKER)
    assert plan.bodies[0].text.startswith("Drafted with AI assistance")
    assert plan.dedupe_key == keys.approve_key("PR_kwDOAAAAAQ", A, "main", None)
    assert "--body-file" in plan.command() and f"--expected-head={A}" in plan.command()
    assert plan.plan_sha256 == hash_of(plan)


def test_an_approve_with_a_body_hashes_the_step_one_text_into_the_key() -> None:
    plan = build_text_plan(
        context(), action="approve", text="lgtm\r\n", head_sha=A, base_ref="main"
    )
    assert plan.dedupe_key == keys.approve_key("PR_kwDOAAAAAQ", A, "main", "lgtm\n")
    assert "lgtm" in plan.bodies[0].text


def test_whitespace_and_trailing_newlines_render_as_given() -> None:
    plain = build_text_plan(context(), action="comment", text="x", head_sha=A, base_ref="main")
    spaced = build_text_plan(context(), action="comment", text="x   ", head_sha=A, base_ref="main")
    newlines = build_text_plan(
        context(), action="comment", text="x\n\n", head_sha=A, base_ref="main"
    )
    assert spaced.plan_sha256 != plain.plan_sha256
    assert "x   \n\n" + MARKER in spaced.bodies[0].text
    # trailing line feeds of a part never add blank lines (body structure)
    assert newlines.bodies[0].text == plain.bodies[0].text
    # but the key sees the step-1 text, so the keys differ
    assert newlines.dedupe_key != plain.dedupe_key


def test_the_text_plan_refuses_what_the_key_refuses() -> None:
    with pytest.raises(ValueError):
        build_text_plan(context(), action="comment", text="a\x07b", head_sha=A, base_ref="main")
    with pytest.raises(ValueError):
        build_text_plan(context(), action="comment", text=None, head_sha=A, base_ref="main")
    with pytest.raises(ValueError):
        build_text_plan(
            context(), action="comment", text="x", head_sha=A, base_ref="main", to="gh:ic:IC_a"
        )
    with pytest.raises(ValueError):
        build_text_plan(context(), action="reply", text="x", head_sha=A, base_ref="main")


def test_unicode_and_large_bodies_are_carried() -> None:
    text = "héllo 日本語 \U0001f600 " + "x" * 50_000
    plan = build_text_plan(context(), action="comment", text=text, head_sha=A, base_ref="main")
    assert text in plan.bodies[0].text
    assert plan.bodies[0].bytes == len(plan.bodies[0].text.encode())
    assert plan.input.decode() == plan.bodies[0].text


def test_a_footer_that_renders_to_a_non_letter_is_a_config_error() -> None:
    from pr_comment.config import Footer

    with pytest.raises(ConfigError) as caught:
        review([finding("f1")], repo_settings=settings(footer=Footer(human="{user} did it")))
    assert caught.value.key == "footer.human"


def test_the_document_is_the_dry_run_plan_object() -> None:
    plan = review([finding("f1", start=1, line=3)])
    doc = plan.document()
    assert doc["plan_sha256"] == plan.plan_sha256
    assert doc["bodies"][0] == {
        "role": "body",
        "text": plan.bodies[0].text,
        "bytes": plan.bodies[0].bytes,
    }
    assert doc["inline_comments"] == [
        {
            "finding_id": "f1",
            "path": "a.py",
            "line": 3,
            "start_line": 1,
            "side": "right",
            "role": "inline:f1",
        }
    ]
    assert doc["commands"] == [plan.command()]
    assert doc["follow_ups"] == []


def test_role_of_maps_forgectls_index_to_a_role() -> None:
    plan = review([finding("f1"), finding("f2", "b.py", 2)])
    assert role_of(plan, None) == "body"
    assert role_of(plan, 1) == "inline:f2"
    for bad in (2, -1, True, "0"):
        with pytest.raises(IndexError):
            role_of(plan, bad)  # type: ignore[arg-type]


def test_every_plan_pins_the_merge_base_value_it_was_built_from() -> None:
    # the plan itself never reads the merge-base: it only sees the moves the source carries
    doc = draft_doc([finding("f1", side="left")])
    ok, _ = source_of(doc)
    assert build_review_plan(context(), ok).posted_inline == ("f1",)
    assert BASE_SHA and MB


# --- the inline comment cap (#70) --------------------------------------------------------------


def _many(count: int) -> list[dict[str, Any]]:
    return [finding(f"f{i}", "a.py", i + 1) for i in range(count)]


def test_the_cap_is_forgectls_review_limit() -> None:
    from tests.fake_forgectl import MAX_COMMENTS

    assert MAX_INLINE_COMMENTS == MAX_COMMENTS == 200


def test_200_inline_comments_build_a_plan() -> None:
    assert len(review(_many(200)).inline_comments) == 200


def test_201_inline_comments_are_refused_with_the_fixed_message() -> None:
    with pytest.raises(TooManyInlineComments) as caught:
        review(_many(201))
    assert str(caught.value) == (
        "the review would post more than 200 inline comments, "
        "the most forgectl accepts in one review"
    )
    assert caught.value.count == 201


def test_only_inline_comments_count_toward_the_cap() -> None:
    # file-level findings and findings moved to the body (a path marker) are not inline comments
    findings = _many(200)
    findings += [finding("g1", "b.py", None), finding("g2", "x<U+202E>y.py", 3)]
    plan = review(findings)
    assert len(plan.inline_comments) == 200 and [m.id for m in plan.moved] == ["g2"]


def test_a_finding_moved_by_the_source_is_not_counted() -> None:
    # a left-side finding moves to the body when the merge base changed (source.moves)
    findings = _many(200) + [finding("l1", "a.py", 9, side="left")]
    source, _ = source_of(draft_doc(findings), merge_base="2" * 40)
    plan = build_review_plan(context(), source)
    assert len(plan.inline_comments) == 200
    assert [(m.id, m.reason) for m in plan.moved] == [("l1", "base_changed")]


def test_a_precheck_move_brings_the_count_under_the_cap() -> None:
    plan = review(_many(201), extra={"f0": "invalid_position"})
    assert len(plan.inline_comments) == 200
