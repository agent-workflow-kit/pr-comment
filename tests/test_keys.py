"""Dedupe keys and the plan hash (docs/interface.md: Dedupe keys, Posting plan; issue #21)."""

from __future__ import annotations

import copy
import hashlib
import json
import random
from typing import Any

import pytest

from pr_comment import keys
from pr_comment.grammars import check_dedupe_key, check_plan_hash

DRAFT_ID = "01HZX3K9QJ5N8V2W4Y6A7B8C9D"
HEAD = "a" * 40
HEAD2 = "b" * 40
BASE = "main"
PR = "example.test/team/proj#7"
TO = "gh:rc:abc123"


def review_args(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "pr": PR,
        "action": "review",
        "event": "comment",
        "head_sha": HEAD,
        "base_ref": BASE,
        "dedupe_key": f"prc:rv:{DRAFT_ID}",
        "to": None,
        "actor": "human:alice",
        "decision_ids": ["d1", "d2"],
        "bodies": [
            {"role": "body", "text": "Summary\n\n<!-- pr-comment:v1 -->"},
            {"role": "inline:f1", "text": "first\n\n<!-- pr-comment:v1 -->"},
            {"role": "inline:f2", "text": "second\n\n<!-- pr-comment:v1 -->"},
        ],
        "inline_comments": [
            {
                "finding_id": "f1",
                "path": "src/a.py",
                "line": 10,
                "start_line": None,
                "side": "right",
                "role": "inline:f1",
            },
            {
                "finding_id": "f2",
                "path": "src/b.py",
                "line": 20,
                "start_line": 18,
                "side": "left",
                "role": "inline:f2",
            },
        ],
        "follow_ups": [{"action": "resolve", "thread_id": None}],
    }
    args.update(over)
    return args


def h(**over: Any) -> str:
    return keys.plan_sha256(**review_args(**over))


# ---- dedupe keys ----


def test_interface_test_vector() -> None:
    array = ["PR_kwDOA", "comment", "hi"]
    digest = hashlib.sha256(
        json.dumps(array, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert digest[:20] == "fdacdffe9dad6cfe01a5"
    assert keys.comment_key("PR_kwDOA", "hi") == "prc:comment:fdacdffe9dad6cfe01a5"


def test_golden_keys() -> None:
    assert keys.review_key(DRAFT_ID) == f"prc:rv:{DRAFT_ID}"
    assert keys.review_approve_key(DRAFT_ID, HEAD) == f"prc:ra:{DRAFT_ID}:aaaaaaaaaaaa"
    assert keys.reply_key("PR_kwDOA", TO, "hi") == "prc:reply:4187c46ec7c6b8b86465"
    assert keys.approve_key("PR_kwDOA", HEAD, BASE, "hi") == "prc:approve:d517401896978c204960"
    assert keys.approve_key("PR_kwDOA", HEAD, BASE, None) == "prc:approve:61ca7597631d3f182040"


def test_review_and_review_approve_keys_differ() -> None:
    assert keys.review_key(DRAFT_ID) != keys.review_approve_key(DRAFT_ID, HEAD)


def test_review_approve_key_follows_the_head() -> None:
    assert keys.review_approve_key(DRAFT_ID, HEAD) != keys.review_approve_key(DRAFT_ID, HEAD2)
    assert keys.review_approve_key(DRAFT_ID, HEAD.upper()) == keys.review_approve_key(
        DRAFT_ID, HEAD
    )


def test_review_key_ignores_everything_but_the_draft_id() -> None:
    # No parameter exists for the anchor, the event, the head or the body.
    assert keys.review_key(DRAFT_ID) == keys.review_key(DRAFT_ID)
    with pytest.raises(TypeError):
        keys.review_key(DRAFT_ID, HEAD)  # type: ignore[call-arg]


def test_approve_without_body_is_the_empty_body() -> None:
    assert keys.approve_key("p_1", HEAD, BASE, None) == keys.approve_key("p_1", HEAD, BASE, "")


def test_approve_key_follows_each_input() -> None:
    base = keys.approve_key("p_1", HEAD, BASE, "x")
    assert keys.approve_key("p_2", HEAD, BASE, "x") != base
    assert keys.approve_key("p_1", HEAD2, BASE, "x") != base
    assert keys.approve_key("p_1", HEAD, "dev", "x") != base
    assert keys.approve_key("p_1", HEAD, BASE, "y") != base


def test_actions_are_domain_separated() -> None:
    comment = keys.comment_key("p_1", "x")
    approve = keys.approve_key("p_1", HEAD, BASE, "x")
    reply = keys.reply_key("p_1", TO, "x")
    assert len({comment, approve, reply}) == 3
    assert {k.split(":")[1] for k in (comment, approve, reply)} == {"comment", "approve", "reply"}
    # Same hash input shape for different actions still gives different digests.
    assert comment.split(":")[2] != reply.split(":")[2]


def test_field_boundaries_are_unambiguous() -> None:
    assert keys.reply_key("p_1", "gh:rc:ab", "c") != keys.reply_key("p_1", "gh:rc:a", "bc")
    assert keys.comment_key("p_1", 'a","b') != keys.comment_key("p_1", 'a",b')
    assert keys.comment_key("ab", "c") != keys.comment_key("a", "bc")
    assert keys.approve_key("p_1", HEAD, "ab", "c") != keys.approve_key("p_1", HEAD, "a", "bc")


def test_body_line_separators_are_normalized_by_step_1() -> None:
    lf = keys.comment_key("p_1", "a\nb\nc")
    for sep in ("\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x85", " ", " "):
        assert keys.comment_key("p_1", f"a{sep}b{sep}c") == lf


def test_body_is_otherwise_taken_as_given() -> None:
    base = keys.comment_key("p_1", "café")
    assert keys.comment_key("p_1", "café") != base  # no Unicode normalization
    assert keys.comment_key("p_1", "café ") != base  # whitespace counts
    assert keys.comment_key("p_1", "café\n") != base
    assert keys.comment_key("p_1", "") != keys.comment_key("p_1", " ")


def test_body_hostile_inputs() -> None:
    for body in ("a\tb\n", "\U0001f600" * 5, "x" * 2_000_000, '"\\\\"', "\u202e"):
        key = keys.comment_key("p_1", body)
        check_dedupe_key(key)


@pytest.mark.parametrize("body", ["\x00", "a\x00b", "\x1b[0m", "\x7f", "a\x9fb", "\u0000" * 10])
def test_key_body_with_a_control_character_is_refused_without_echo(body: str) -> None:
    for call in (
        lambda: keys.comment_key("p_1", body),
        lambda: keys.reply_key("p_1", TO, body),
        lambda: keys.approve_key("p_1", HEAD, BASE, body),
    ):
        with pytest.raises(ValueError) as err:
            call()
        assert "\x00" not in str(err.value)


@pytest.mark.parametrize(
    "pr_id", ["", " ", "a b", "a\x00b", "a\nb", "caf\u00e9", "a" * 129, "p#1", "a.b", "a\n"]
)
def test_pr_id_outside_its_grammar_is_refused(pr_id: str) -> None:
    with pytest.raises(ValueError):
        keys.comment_key(pr_id, "x")
    with pytest.raises(ValueError):
        keys.reply_key(pr_id, TO, "x")
    with pytest.raises(ValueError):
        keys.approve_key(pr_id, HEAD, BASE, None)


def test_pr_id_grammar_boundaries() -> None:
    for pr_id in ("a", "a" * 128, "PR_kwDOA", "gl:x/y=z-1"):
        keys.comment_key(pr_id, "x")


@pytest.mark.parametrize("body", ["\ud800", "a\udfffb", "\ud83d"])
def test_lone_surrogate_is_refused_without_echo(body: str) -> None:
    with pytest.raises(ValueError) as err:
        keys.comment_key("p_1", body)
    assert "\ud800" not in str(err.value)


@pytest.mark.parametrize("bad", [None, 1, b"x", ["x"], True])
def test_non_string_inputs_are_refused(bad: Any) -> None:
    with pytest.raises(ValueError):
        keys.comment_key("p_1", bad)
    with pytest.raises(ValueError):
        keys.comment_key(bad, "x")
    with pytest.raises(ValueError):
        keys.review_key(bad)


def test_approve_none_body_only_for_approve() -> None:
    with pytest.raises(ValueError):
        keys.comment_key("p_1", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        keys.reply_key("p_1", TO, None)  # type: ignore[arg-type]


@pytest.mark.parametrize("draft_id", ["", "short", DRAFT_ID.lower(), DRAFT_ID + "0", "I" * 26])
def test_bad_draft_id_is_refused(draft_id: str) -> None:
    with pytest.raises(ValueError):
        keys.review_key(draft_id)
    with pytest.raises(ValueError):
        keys.review_approve_key(draft_id, HEAD)


@pytest.mark.parametrize("head", ["", "abc", "g" * 40, "a" * 41, "a" * 40 + "\n"])
def test_bad_head_is_refused(head: str) -> None:
    with pytest.raises(ValueError):
        keys.review_approve_key(DRAFT_ID, head)
    with pytest.raises(ValueError):
        keys.approve_key("p_1", head, BASE, None)


def test_bad_base_to_and_pr_are_refused() -> None:
    with pytest.raises(ValueError):
        keys.approve_key("p_1", HEAD, "-x", None)
    with pytest.raises(ValueError):
        keys.reply_key("p_1", "nope", "x")
    with pytest.raises(ValueError):
        keys.comment_key("", "x")


def test_every_key_satisfies_the_dedupe_key_grammar() -> None:
    rng = random.Random(21)
    for _ in range(200):
        body = "".join(
            chr(rng.choice([rng.randrange(0xA0, 0xD800), rng.randrange(0xE000, 0x110000)]))
            for _ in range(rng.randrange(0, 40))
        )
        for key in (
            keys.comment_key("p_1", body),
            keys.reply_key("p_1", TO, body),
            keys.approve_key("p_1", HEAD, BASE, body),
        ):
            assert check_dedupe_key(key) == key
    assert len(keys.comment_key("p_1", "x")) == len("prc:comment:") + 20
    check_dedupe_key(keys.review_key(DRAFT_ID))
    check_dedupe_key(keys.review_approve_key(DRAFT_ID, HEAD))


def test_keys_are_deterministic_across_calls() -> None:
    assert [keys.comment_key("p_1", "x") for _ in range(3)] == [keys.comment_key("p_1", "x")] * 3


# ---- plan hash ----


def test_plan_hash_shape_and_grammar() -> None:
    assert check_plan_hash(h()) == h()


def test_plan_hash_matches_the_spec_construction() -> None:
    args = review_args()
    obj = {
        "pr": args["pr"],
        "action": args["action"],
        "event": args["event"],
        "head_sha": args["head_sha"],
        "base_ref": args["base_ref"],
        "dedupe_key": args["dedupe_key"],
        "to": None,
        "actor": args["actor"],
        "decision_id": args["decision_ids"],
        "bodies": args["bodies"],
        "inline_comments": args["inline_comments"],
        "follow_ups": args["follow_ups"],
    }
    expected = hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert h() == expected


def test_plan_hash_golden_vector() -> None:
    assert h() == GOLDEN_PLAN


GOLDEN_PLAN = "2a941508b67611f60b55ffb701fa488acbecd7edae7fbe607bfef51b347f112e"


@pytest.mark.parametrize(
    "over",
    [
        {"pr": "example.test/team/proj#8"},
        {"action": "approve", "event": "approve"},
        {"event": "request_changes"},
        {"head_sha": HEAD2},
        {"base_ref": "dev"},
        {"dedupe_key": "prc:rv:other"},
        {"actor": "human:bob"},
        {"actor": "auto:alice"},
        {"decision_ids": ["d1"]},
        {"decision_ids": ["d2", "d1"]},
        {"decision_ids": ["d1", "d2", "d3"]},
        {"decision_ids": []},
        {"follow_ups": []},
        {"follow_ups": [{"action": "resolve", "thread_id": "gl:d:" + "a" * 40}]},
    ],
)
def test_plan_hash_changes_with_each_top_level_input(over: dict[str, Any]) -> None:
    assert h(**over) != h()


def mutate_paths() -> list[tuple[str, Any]]:
    return [
        ("body_text", lambda a: a["bodies"][0].__setitem__("text", "Summary!")),
        ("inline_text", lambda a: a["bodies"][1].__setitem__("text", "first!")),
        (
            "body_text_ws",
            lambda a: a["bodies"][0].__setitem__("text", "Summary\n\n<!-- pr-comment:v1 -->\n"),
        ),
        (
            "body_swap",
            lambda a: (
                a["bodies"].__setitem__(slice(1, 3), [a["bodies"][2], a["bodies"][1]]),
                a["inline_comments"].reverse(),
            ),
        ),
        ("drop_body", lambda a: a["bodies"].pop()),
        ("path", lambda a: a["inline_comments"][0].__setitem__("path", "src/c.py")),
        ("line", lambda a: a["inline_comments"][0].__setitem__("line", 11)),
        ("start_line", lambda a: a["inline_comments"][0].__setitem__("start_line", 9)),
        ("start_line_none", lambda a: a["inline_comments"][1].__setitem__("start_line", None)),
        ("side", lambda a: a["inline_comments"][0].__setitem__("side", "left")),
        ("finding_id", lambda a: a["inline_comments"][1].__setitem__("finding_id", "f9")),
        (
            "inline_order",
            lambda a: (
                a["inline_comments"].reverse(),
                a["bodies"].__setitem__(slice(1, 3), [a["bodies"][2], a["bodies"][1]]),
            ),
        ),
    ]


@pytest.mark.parametrize("name", [n for n, _ in mutate_paths()])
def test_plan_hash_changes_with_each_body_and_inline_field(name: str) -> None:
    args = review_args()
    dict(mutate_paths())[name](args)
    if name == "finding_id":
        args["bodies"][2]["role"] = "inline:f9"
        args["inline_comments"][1]["role"] = "inline:f9"
    if name == "drop_body":
        args["inline_comments"].pop()
    assert keys.plan_sha256(**args) != h()


def test_plan_hash_ignores_what_is_not_confirmed() -> None:
    args = review_args()
    for body in args["bodies"]:
        body["bytes"] = 999  # the plan output's extra field
    assert keys.plan_sha256(**args) == h()
    assert h(decision_ids=("d1", "d2")) == h()  # a tuple is the same list


def test_plan_hash_ignores_mapping_key_order() -> None:
    args = review_args()
    args["bodies"] = [dict(reversed(list(b.items()))) for b in args["bodies"]]
    args["inline_comments"] = [dict(reversed(list(c.items()))) for c in args["inline_comments"]]
    assert keys.plan_sha256(**args) == h()


def test_plan_hash_does_not_mutate_its_input() -> None:
    args = review_args()
    before = copy.deepcopy(args)
    keys.plan_sha256(**args)
    assert args == before


def test_plan_hash_is_exact_about_text() -> None:
    base = h()
    for text in ("Summary\n\n<!-- pr-comment:v1 -->\n", "Summary\t\n\n<!-- pr-comment:v1 -->"):
        args = review_args()
        args["bodies"][0]["text"] = text
        assert keys.plan_sha256(**args) != base
    args = review_args()
    args["bodies"][0]["text"] = "café"
    nfc = keys.plan_sha256(**args)
    args["bodies"][0]["text"] = "café"
    assert keys.plan_sha256(**args) != nfc


def test_plan_hash_has_no_ascii_escape_ambiguity() -> None:
    args = review_args()
    args["bodies"][0]["text"] = "é"
    a = keys.plan_sha256(**args)
    args["bodies"][0]["text"] = "\\u00e9"
    assert keys.plan_sha256(**args) != a


def test_plan_hash_hostile_texts() -> None:
    for text in ("x" * 3_000_000, "\U0001f600‮", '"}],"to":"x', "a\tb\n"):
        args = review_args()
        args["bodies"][0]["text"] = text
        check_plan_hash(keys.plan_sha256(**args))


@pytest.mark.parametrize("text", ["\x00", "a\rb", "a\x0bb", "\x1b[0m", "\x7f", "a\x85b"])
def test_plan_hash_refuses_a_control_character_in_text_without_echo(text: str) -> None:
    for index in (0, 1):
        args = review_args()
        args["bodies"][index]["text"] = text
        with pytest.raises(ValueError) as err:
            keys.plan_sha256(**args)
        assert "\x00" not in str(err.value)


@pytest.mark.parametrize("bad", ["\ud800", "a\udc00"])
def test_plan_hash_refuses_a_lone_surrogate_without_echo(bad: str) -> None:
    for field in ("text", "path"):
        args = review_args()
        if field == "text":
            args["bodies"][0]["text"] = bad
        else:
            args["inline_comments"][0]["path"] = bad
        with pytest.raises(ValueError) as err:
            keys.plan_sha256(**args)
        assert bad not in str(err.value)


def test_plan_hash_int_vs_float_and_bool() -> None:
    for bad in (10.0, True, "10", None, 0, -1, 2**63 if False else 1.5):
        args = review_args()
        args["inline_comments"][0]["line"] = bad
        with pytest.raises(ValueError):
            keys.plan_sha256(**args)
    for bad in (18.0, True, "18", 0):
        args = review_args()
        args["inline_comments"][1]["start_line"] = bad
        with pytest.raises(ValueError):
            keys.plan_sha256(**args)


def test_plan_hash_refuses_malformed_plans() -> None:
    bad_cases: list[dict[str, Any]] = [
        {"action": "resolve"},
        {"event": "approved"},
        {"head_sha": "abc"},
        {"base_ref": None},
        {"actor": "bob"},
        {"dedupe_key": "has space"},
        {"decision_ids": "d1"},
        {"decision_ids": ["bad id"]},
        {"decision_ids": [f"d{i}" for i in range(33)]},
        {"to": 5},
        {"bodies": []},
        {"bodies": "x"},
        {"bodies": [{"role": "inline:f1", "text": "x"}]},
        {"bodies": [{"role": "body"}]},
        {"bodies": [{"role": "body", "text": 5}]},
        {"follow_ups": [{"action": "post", "thread_id": None}]},
        {"follow_ups": [{"action": "resolve"}]},
        {"follow_ups": "x"},
        {"inline_comments": [{"finding_id": "f1"}]},
    ]
    for over in bad_cases:
        with pytest.raises(ValueError):
            h(**over)


def test_plan_hash_requires_the_canonical_pr() -> None:
    for pr in (
        "https://example.test/team/proj/pull/7",
        "proj#7",
        "Example.test/team/proj#7",
        "example.test/team/proj#0",
        "example.test/team/../proj#7",
        "example.test/team/proj#7\n",
    ):
        with pytest.raises(ValueError):
            h(pr=pr)
    h(pr="example.test:8080/a/b.c-d_e#1234567890")


def test_plan_hash_action_event_to_consistency() -> None:
    comment_base = {"bodies": review_args()["bodies"][:1], "inline_comments": []}
    bad: list[dict[str, Any]] = [
        {"event": None},  # review needs an event
        {"to": TO},  # to only for reply
        {"action": "reply", "event": None, "to": None, **comment_base},
        {"action": "reply", "event": "comment", "to": TO, **comment_base},
        {"action": "comment", "event": "comment", **comment_base},
        {"action": "comment", "event": None, "to": TO, **comment_base},
        {"action": "approve", "event": None, **comment_base},
        {"action": "approve", "event": "comment", **comment_base},
        {"action": "approve", "event": "approve", "to": TO, **comment_base},
    ]
    for over in bad:
        with pytest.raises(ValueError):
            h(**over)
    for over in (
        {"action": "reply", "event": None, "to": TO, **comment_base},
        {"action": "comment", "event": None, **comment_base},
        {"action": "approve", "event": "approve", **comment_base},
        {"event": "approve"},
        {"event": "request_changes"},
    ):
        h(**over)


def test_plan_hash_does_not_tie_the_key_prefix_to_the_action() -> None:
    h(dedupe_key="custom-key")
    h(action="review", dedupe_key="prc:comment:" + "a" * 20)


def test_plan_hash_inline_roles_must_match_bodies_exactly() -> None:
    dup = review_args()
    dup["bodies"].append({"role": "inline:f1", "text": "again"})
    dup["inline_comments"].append(copy.deepcopy(dup["inline_comments"][0]))
    orphan_body = review_args()
    orphan_body["inline_comments"].pop()
    orphan_comment = review_args()
    orphan_comment["bodies"].pop()
    reordered = review_args()
    reordered["inline_comments"].reverse()
    for args in (dup, orphan_body, orphan_comment, reordered):
        with pytest.raises(ValueError):
            keys.plan_sha256(**args)


def test_plan_hash_start_line_must_be_less_than_line() -> None:
    for start, line in ((11, 10), (10, 10)):
        args = review_args()
        args["inline_comments"][0]["start_line"] = start
        args["inline_comments"][0]["line"] = line
        with pytest.raises(ValueError):
            keys.plan_sha256(**args)
    args = review_args()
    args["inline_comments"][0]["start_line"] = 9
    keys.plan_sha256(**args)


def test_plan_hash_line_cap() -> None:
    top = 2**31 - 1
    args = review_args()
    args["inline_comments"][0]["line"] = top
    args["inline_comments"][0]["start_line"] = top - 1
    keys.plan_sha256(**args)
    for field in ("line", "start_line"):
        args = review_args()
        args["inline_comments"][1][field] = top + 1
        with pytest.raises(ValueError):
            keys.plan_sha256(**args)


def test_plan_hash_refuses_inconsistent_roles() -> None:
    args = review_args()
    args["inline_comments"][0]["role"] = "inline:f2"
    with pytest.raises(ValueError):
        keys.plan_sha256(**args)
    args = review_args()
    args["bodies"][1]["role"] = "inline:zzz"
    with pytest.raises(ValueError):
        keys.plan_sha256(**args)
    args = review_args()
    args["bodies"].append({"role": "body", "text": "again"})
    with pytest.raises(ValueError):
        keys.plan_sha256(**args)


def test_plan_hash_for_comment_reply_approve() -> None:
    base = {
        "pr": PR,
        "head_sha": HEAD,
        "base_ref": BASE,
        "actor": "auto:bot",
        "decision_ids": [],
        "bodies": [{"role": "body", "text": "hi\n\n<!-- pr-comment:v1 -->"}],
        "inline_comments": [],
        "follow_ups": [],
    }
    comment = keys.plan_sha256(
        **base, action="comment", event=None, dedupe_key=keys.comment_key("PR_kwDOA", "hi"), to=None
    )
    reply = keys.plan_sha256(
        **base,
        action="reply",
        event=None,
        dedupe_key=keys.reply_key("PR_kwDOA", TO, "hi"),
        to=TO,
    )
    approve = keys.plan_sha256(
        **base,
        action="approve",
        event="approve",
        dedupe_key=keys.approve_key("PR_kwDOA", HEAD, BASE, "hi"),
        to=None,
    )
    assert len({comment, reply, approve}) == 3


def test_plan_hash_random_drafts_property() -> None:
    rng = random.Random(2121)
    words = ["a", "b", "x y", "é", "\U0001f600", "q\n", " ", "", "\t", "<!-- m -->"]

    def make() -> dict[str, Any]:
        n = rng.randrange(0, 5)
        ids = [f"f{i}" for i in range(n)]
        return review_args(
            bodies=[{"role": "body", "text": rng.choice(words) + rng.choice(words)}]
            + [
                {"role": f"inline:{i}", "text": rng.choice(words) + str(k)}
                for k, i in enumerate(ids)
            ],
            inline_comments=[
                {
                    "finding_id": i,
                    "path": rng.choice(["a.py", "b/c.py", "d e.py"]),
                    "line": rng.randrange(2, 500),
                    "start_line": rng.choice([None, 1]),
                    "side": rng.choice(["left", "right"]),
                    "role": f"inline:{i}",
                }
                for i in ids
            ],
            decision_ids=[f"d{rng.randrange(9)}" for _ in range(rng.randrange(0, 4))],
        )

    for _ in range(150):
        args = make()
        digest = keys.plan_sha256(**args)
        # Irrelevant: key order of every mapping, extra fields.
        shuffled = copy.deepcopy(args)
        for group in ("bodies", "inline_comments", "follow_ups"):
            shuffled[group] = [
                dict(rng.sample(list(m.items()), len(m))) | {"extra": rng.random()}
                for m in shuffled[group]
            ]
        assert keys.plan_sha256(**shuffled) == digest
        # Relevant: every single scalar field changed alone changes the hash.
        for i in range(len(args["bodies"])):
            changed = copy.deepcopy(args)
            changed["bodies"][i]["text"] += "x"
            assert keys.plan_sha256(**changed) != digest
        for i, comment in enumerate(args["inline_comments"]):
            for field, value in (("path", "zz.py"), ("line", comment["line"] + 1)):
                changed = copy.deepcopy(args)
                changed["inline_comments"][i][field] = value
                assert keys.plan_sha256(**changed) != digest
