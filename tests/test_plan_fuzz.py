"""Seeded property tests of the posting plan: for generated hostile drafts and body files the plan
is deterministic, its hash is the hash of its fields, every body ends with the marker, and the
text pr-comment hands over is a fixed point of forgectl's neutralizing (the pinned fake reports 0
``neutralized_lines`` and 0 ``markers_removed`` for it, so the plan is what is posted)."""

from __future__ import annotations

import random
from typing import Any

import pytest

from pr_comment import keys, textpipe
from pr_comment.assemble import forgectl_neutralize
from pr_comment.draft import text_issues, validate_document
from pr_comment.plan import Plan, build_review_plan, build_text_plan
from tests.posting import (
    A,
    context,
    draft_doc,
    finding,
    prepared,
    source_of,
)

FRAGMENTS = [
    "plain words",
    "/approve",
    "/lgtm now",
    "@zed",
    "@org/team",
    "a@b.com",
    "&#64;amy",
    "&commat;bob",
    "&amp@carl",
    "<b>x</b>",
    "<!-- c -->",
    "![i](http://e.example/x.png)",
    "forgectl:dedupe:key",
    "FORGECTL:DEDUPE:KEY",
    "<!-- forgectl:dedupe:abc -->",
    "<!-- pr-comment:v1 -->",
    "```",
    "~~~",
    "````",
    "    indented",
    "> quote",
    ">>>",
    "- item",
    "1. one",
    " ",
    "\x85",
    "\x0b",
    "\r\n",
    "\r",
    "\t",
    "‮",
    "​",
    "日本語",
    "\U0001f600",
    "x" * 300,
    " ",
    "\n",
    "\n\n",
    "<U+202E>",
    "<run split>",
    "https://example.org/@user",
    "www.example.org",
    "mailto:a@b.c",
]
SEEDS = list(range(40))


def one_line(value: str) -> str:
    return textpipe.normalize_lines(value).replace("\n", " ")


def text(rng: random.Random, *, lines: int = 4) -> str:
    return "".join(rng.choice(FRAGMENTS) + rng.choice(["", " ", "\n"]) for _ in range(lines))


def generated(seed: int) -> dict[str, Any] | None:
    rng = random.Random(seed)
    findings: list[dict[str, Any]] = []
    for i in range(rng.randint(1, 5)):
        line = None if rng.random() < 0.2 else rng.randint(1, 50)
        side = rng.choice([None, "left", "right"]) if line is not None else None
        extra: dict[str, Any] = {}
        if line is not None and side != "left" and rng.random() < 0.5:
            count = rng.randint(0, 3)
            extra["suggestion"] = {"lines": [one_line(rng.choice(FRAGMENTS)) for _ in range(count)]}
        if rng.random() < 0.3:
            extra["title"] = one_line(text(rng, lines=1))[:200]
        if rng.random() < 0.3:
            extra["severity"] = rng.choice(["blocker", "major", "minor", "nit", "info"])
        findings.append(
            finding(
                f"f{i}",
                rng.choice(["a.py", "dir/b.py", "c d.py", "日本.py"]),
                line,
                side=side,
                body=text(rng),
                **extra,
            )
        )
    doc = draft_doc([], body=text(rng))
    doc["findings"] = findings
    if validate_document(doc) or text_issues(doc):
        return None  # the draft loader refuses it before any plan exists
    return doc


def review_plan(doc: dict[str, Any], **ctx: Any) -> Plan:
    source, _ = source_of(doc)
    return build_review_plan(context(**ctx), source)


@pytest.mark.parametrize("seed", SEEDS)
def test_a_generated_review_plan_is_deterministic_and_posts_what_forgectl_would(seed: int) -> None:
    doc = generated(seed)
    if doc is None:
        pytest.skip("the draft is refused before a plan")
    one = review_plan(doc)
    two = review_plan(doc)
    assert one == two and one.plan_sha256 == two.plan_sha256 and one.input == two.input
    assert one.plan_sha256 == keys.plan_sha256(
        pr=one.pr,
        action=one.action,
        event=one.event,
        head_sha=one.head_sha,
        base_ref=one.base_ref,
        dedupe_key=one.dedupe_key,
        to=None,
        actor=one.actor,
        decision_ids=one.decision_ids,
        bodies=[{"role": b.role, "text": b.text} for b in one.bodies],
        inline_comments=[
            {
                "finding_id": c.finding_id,
                "path": c.path,
                "line": c.line,
                "start_line": c.start_line,
                "side": c.side,
                "role": c.role,
            }
            for c in one.inline_comments
        ],
        follow_ups=[],
    )
    for body in one.bodies:
        assert body.text.rstrip("\n").endswith("<!-- pr-comment:v1 -->")
        # the text is a fixed point of forgectl's own neutralizing
        assert forgectl_neutralize(body.text).text == body.text
        assert forgectl_neutralize(body.text).markers_removed == 0
    prep = prepared(one)
    assert prep.markers_removed == 0
    assert prep.neutralized_lines == 0


@pytest.mark.parametrize("seed", SEEDS)
def test_a_generated_comment_plan_is_a_fixed_point_of_forgectls_neutralizing(seed: int) -> None:
    rng = random.Random(1000 + seed)
    body = text(rng, lines=6)
    if not body.strip():
        pytest.skip("an empty body is refused before a plan")
    try:
        one = build_text_plan(context(), action="comment", text=body, head_sha=A, base_ref="main")
    except ValueError:
        pytest.skip("a control character is refused before a plan")
    two = build_text_plan(context(), action="comment", text=body, head_sha=A, base_ref="main")
    assert one == two
    prep = prepared(one)
    assert prep.neutralized_lines == 0 and prep.markers_removed == 0


@pytest.mark.parametrize("seed", SEEDS[:15])
def test_the_hash_moves_with_every_posted_byte(seed: int) -> None:
    doc = generated(seed)
    if doc is None:
        pytest.skip("the draft is refused before a plan")
    base = review_plan(doc)
    changed = dict(doc)
    changed["body"] = doc["body"] + "x"
    assert review_plan(changed).plan_sha256 != base.plan_sha256
    other_key = review_plan(doc, dedupe_key="another-key")
    assert other_key.plan_sha256 != base.plan_sha256
