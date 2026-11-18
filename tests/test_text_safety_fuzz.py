"""Differential fuzz of the whole text pipeline against GitHub's engine (issue #22).

Random drafts and body files are built from a token alphabet (fences, quotes, lists, tabs, tables,
definitions, entities, backslashes, backticks, line separators, mentions, HTML, images, markers),
run through the real modules and judged by the rendered-output oracle (``tests/textsafety.py``):
no raw HTML, no image, no live mention, no applicable suggestion pr-comment did not write, the
attribution line first, markers visible in code. Input the pipeline refuses is skipped (and
counted: most input must get through, or the fuzz proves nothing).

Budget: ``CASES`` drafts from fixed seeds in ``make verify``, deterministic. A deeper run is opt-in:

    TEXT_SAFETY_FUZZ_CASES=200000 TEXT_SAFETY_FUZZ_SEED=7 uv run pytest -m fuzz_long

A failing draft is shrunk and reported as JSON; save it as a new case of
``tests/test_text_safety.py`` (then regenerate that golden file) and fix the cause.
"""

from __future__ import annotations

import os
import random
import re
from typing import Any

import pytest

from pr_comment import assemble, render, textpipe
from pr_comment.mentions import MentionNeutralizer
from tests import textsafety
from tests.textsafety import (
    Refused,
    draft_json,
    live_mentions,
    make_document,
    make_unanchored,
    parse,
    render_probe,
    render_safe,
    run_review,
    run_text_command,
    shrink,
    violations,
)

CASES = 6000
SEEDS = (22, 2210, 220617, 20260617)

# Tokens that can appear in a multi-line text. Weighted by repetition rather than by a table.
TEXT_TOKENS = [
    # mentions and their disguises
    "@", "@victim", "@org/team", "@all", "a@b.example", "x@", "&#64;", "&#064;", "&#x40;",
    "&#x40", "&commat;", "&commat", "&amp", "&#38", "&#x26", "&", ";", "\\@", "＠",
    # code
    "`", "``", "```", "````", "~~~", "~~~~", "```mermaid", "~~~mermaid", "```text", "    ", "\t",
    # blocks
    ">", "> ", ">>>", "- ", "* ", "+ ", "1. ", "1) ", "# ", "## ", "---", "===", "***", "|",
    "| a |", "|---|", ":", "[a]: /u", "[r]: https://t.example/pic", "[r]:", "[^1]", "[^1]: n",
    "- [ ] ", "<U+202E>", "<run split>", "<!-- pr-comment:v1 -->", "forgectl:dedupe:x", "/ping",
    "/",
    # inline
    "*", "**", "_", "__", "~~", "(", ")", "[", "]", "](", "![", "!", "![t][r]", "[t][r]", "\\",
    "\\\\", "![a](https://t.example/p)", "![r]", "<", "<img src=x>", "<details>",
    "</details>", "<!--", "-->", "<script>", "</", "<br>", "<https://example.org>",
    "https://example.org/@victim", "www.example.org", "javascript:alert(1)", "&lt;", "&#60;",
    "&#33;", "&amp;#64;",
    # text, whitespace, separators
    "a", "x", "1", "é", "\u200b", "\u202e", "word", " ", " ", "\n", "\n", "\n\n", "\r\n",
    "\r", "\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\x1c", "\x1e",
]  # fmt: skip
# Rare, and refused by the pipeline: the fuzz must see refusals too (a suggestion fence is
# refused, and the entity-encoded one is only text to a Markdown parser).
REFUSED_TOKENS = ["\x00", "\x1b", "\x7f", "\x9f", "\ud800"]
FENCE_TOKENS = [
    "```suggestion", "~~~ suggestion", "``` suggestion", "````suggestion:-1+0", "```\\suggestion",
    "```&#115;uggestion", "&#96;&#96;&#96;suggestion", "~~~Suggestion", "```text suggestion",
    "> ```suggestion", "- ~~~suggestion",
]  # fmt: skip
ONE_LINE_TOKENS = [
    t for t in TEXT_TOKENS if not any(c in t for c in "\n\r\x0b\x0c\x85\x1c\x1d\x1e\u2028\u2029")
]
PATH_TOKENS = [
    "src/",
    "a.py",
    "b",
    "`",
    "``",
    "[x](u)",
    " ",
    "\u202e",
    "\u200b",
    "<U+",
    "@",
    "&#64;",
    "<",
    "![",
]
ACTORS = ["human:alice", "auto:policy-1", "auto:@all", "human:www.example.org", "human:a@b"]
FOOTERS = [
    "Drafted with AI assistance · posted by {user} after review · {sha}",
    "Reviewed by {user} {policy} {decision_id} v{tool_version} {sha} {{x}}",
    "Posted by {user}: @all /x !y {sha}",
    "A {user}",
    False,
]


def text(rng: random.Random, tokens: list[str], low: int = 0, high: int = 24) -> str:
    out = [rng.choice(tokens) for _ in range(rng.randint(low, high))]
    if rng.random() < 0.006:
        out.insert(rng.randint(0, len(out)), rng.choice(REFUSED_TOKENS))
    if rng.random() < 0.02:
        out.insert(rng.randint(0, len(out)), rng.choice(FENCE_TOKENS))
    return "".join(out)


def one_line(rng: random.Random, tokens: list[str], low: int = 0, high: int = 10) -> str:
    return text(rng, tokens, low, high)


def finding(rng: random.Random, fid: str, *, anchored: bool) -> dict[str, Any]:
    kind = rng.choice(
        ["inline", "inline", "range", "file", "left"] if anchored else ["inline", "file"]
    )
    f: dict[str, Any] = {
        "id": fid,
        "path": "src/" + one_line(rng, PATH_TOKENS, 0, 5) + "a",
        "line": None if kind == "file" else rng.randint(2, 30),
        "body": text(rng, TEXT_TOKENS, 1, 24) or "x",
    }
    if kind == "range":
        f["start_line"] = rng.randint(1, f["line"] - 1)
    if kind == "left":
        f["side"] = "left"
    if rng.random() < 0.5:
        f["severity"] = rng.choice(["blocker", "major", "minor", "nit", "info"])
    if rng.random() < 0.5:
        f["title"] = one_line(rng, ONE_LINE_TOKENS)
    if kind in ("inline", "range") and rng.random() < 0.6:
        f["suggestion"] = {
            "lines": [one_line(rng, ONE_LINE_TOKENS, 0, 8) for _ in range(rng.randint(0, 3))]
        }
    return f


def document(rng: random.Random) -> tuple[dict[str, Any], dict[str, Any]]:
    findings = [finding(rng, f"f{i}", anchored=True) for i in range(rng.randint(0, 4))]
    unanchored = [
        make_unanchored(
            finding(rng, f"u{i}", anchored=False), rng.choice(["changed", "file_removed"])
        )
        for i in range(rng.randint(0, 2))
    ]
    doc = make_document(body=text(rng, TEXT_TOKENS), findings=findings, unanchored=unanchored)
    moved = [f["id"] for f in findings if f["line"] is not None and rng.random() < 0.2]
    options = {
        "actor": rng.choice(ACTORS),
        "host": rng.choice(["github", "gitlab"]),
        "moved": moved,
        "footer_human": rng.choice(FOOTERS),
    }
    return doc, options


def failing(doc: dict[str, Any], options: dict[str, Any]) -> bool:
    try:
        posted = run_review(doc, **options)
    except Refused:
        return False
    return any(violations(p.text, p.expect) for p in posted)


def report(doc: dict[str, Any], options: dict[str, Any]) -> str:
    small = shrink(doc, lambda candidate: failing(candidate, options))
    problems = [
        (p.label, violations(p.text, p.expect))
        for p in run_review(small, **options)
        if violations(p.text, p.expect)
    ]
    return (
        "text-safety fuzz found a body that breaks the oracle; save this draft as a new case "
        f"of tests/test_text_safety.py\noptions: {options!r}\nviolations: {problems!r}\n"
        f"{draft_json(small)}"
    )


def fuzz(seed: int, cases: int) -> tuple[int, int]:
    """Runs ``cases`` random drafts and body files; returns (bodies checked, refused)."""
    rng = random.Random(seed)
    checked = refused = 0
    for _ in range(cases):
        if rng.random() < 0.15:
            body = text(rng, TEXT_TOKENS, 0, 30)
            options: dict[str, Any] = {
                "actor": rng.choice(ACTORS),
                "footer_human": rng.choice(FOOTERS),
            }
            try:
                posted = run_text_command(body, **options)
            except Refused:
                refused += 1
                continue
            for p in posted:
                checked += 1
                problems = violations(p.text, p.expect)
                if problems:
                    pytest.fail(f"body file {body!r} {options!r}: {problems}", pytrace=False)
            continue
        doc, options = document(rng)
        try:
            posted = run_review(doc, **options)
        except Refused:
            refused += 1
            continue
        for p in posted:
            checked += 1
            if violations(p.text, p.expect):
                pytest.fail(report(doc, options), pytrace=False)
    return checked, refused


@pytest.mark.parametrize("seed", SEEDS)
def test_differential_fuzz_with_fixed_seeds(seed: int) -> None:
    checked, refused = fuzz(seed, CASES // len(SEEDS))
    # most input gets through; a fuzz that is mostly refused checks nothing
    assert checked > 3 * refused, (checked, refused)
    assert refused > 0, "the refusals are part of what is fuzzed"


def test_the_fuzz_alphabet_reaches_the_dangerous_constructs() -> None:
    """The generator must produce what the oracle exists for, else a green run means little."""
    rng = random.Random(1)
    seen: set[str] = set()
    for _ in range(300):
        doc, options = document(rng)
        try:
            posted = run_review(doc, **options)
        except Refused as err:
            seen.add("refused:" + err.kind)
            continue
        for p in posted:
            t = p.text
            if "```suggestion" in t:
                seen.add("applicable suggestion")
            if "_Suggestion shown as code" in t:
                seen.add("degraded suggestion")
            if "<run split>" in t:
                seen.add("run split")
            if "<U+" in t:
                seen.add("marker")
            if "＜" in t:
                seen.add("neutralized <")
            if "！[" in t:
                seen.add("neutralized image")
            if "＠" in t:
                seen.add("neutralized mention")
            if "### Findings that could not be anchored" in t:
                seen.add("moved section")
            if "### File-level findings" in t:
                seen.add("file-level section")
    assert {
        "applicable suggestion",
        "degraded suggestion",
        "run split",
        "marker",
        "neutralized <",
        "neutralized image",
        "neutralized mention",
        "moved section",
        "file-level section",
        "refused:suggestion_fence",
        "refused:control_character",
    } <= seen, seen


def test_the_shrinker_reduces_a_failing_draft_and_keeps_it_failing() -> None:
    # Without the pipeline's neutralizing the oracle fails; stand in a predicate that does too.
    doc = make_document(
        body="intro ```\n@x\n```",
        findings=[{"id": "f1", "path": "src/a", "line": 3, "body": "text MARK more"}],
    )

    def has_mark(candidate: dict[str, Any]) -> bool:
        return "MARK" in candidate["findings"][0]["body"] if candidate["findings"] else False

    small = shrink(doc, has_mark)
    assert small["findings"][0]["body"] == "MARK"
    assert small["body"] == ""


@pytest.mark.fuzz_long
def test_long_differential_fuzz() -> None:
    cases = int(os.environ.get("TEXT_SAFETY_FUZZ_CASES", "200000"))
    seed = int(os.environ.get("TEXT_SAFETY_FUZZ_SEED", "1"))
    checked, refused = fuzz(seed, cases)
    assert checked > 3 * refused, (checked, refused)


RAW_HTML_TOKENS = [
    "<div>", "</div>", "<p>", "<span>", "<a href=x>", "<!--", "-->", "<script>", "<details>",
    "<pre>", "<code>", "<table>", "<td>", "&amp", "&#38", "&#x26", "&lt", "&copy", "&", "&#",
    "@", "@victim", "@org/team", "a", "1", "\n", "\n\n", " ", "<br/>", "\t", "    ", "> ", "- ",
]  # fmt: skip


def test_the_mention_pass_alone_is_live_free_in_raw_html_contexts() -> None:
    """Without the ``<`` pass raw HTML survives, cmark copies a raw HTML block verbatim and a
    downstream parser decodes it the HTML5 way (an `@` after an unterminated entity-like tail is
    live there). The mention pass must hold on its own, as the character pass can change."""
    rng = random.Random(16)
    for _ in range(3000):
        raw = "".join(rng.choice(RAW_HTML_TOKENS) for _ in range(rng.randint(1, 20)))
        out = MentionNeutralizer().neutralize(raw)
        for rendered in (render_safe(out), render_probe(out)):
            assert live_mentions(parse(rendered)) == [], (raw, out)


def test_that_oracle_flags_the_mention_pass_without_the_entity_tail_rule() -> None:
    raw = "<div>&amp@victim</div>"
    weak = re.sub(r"(?<![A-Za-z0-9])@", "＠", raw)
    assert live_mentions(parse(render_probe(weak))) != []


# --- the fuzz can fail: each neutralizing step, broken on purpose, is caught --------------------


def _no_image_pass(text: str) -> str:
    return text.replace("<", "＜")


def _no_lt_pass(text: str) -> str:
    return text.replace("![", "！[")


def _literal_only(self: MentionNeutralizer, text: str) -> str:
    return re.sub(r"(?<![A-Za-z0-9])@", "＠", text)


def _no_entity_tail(self: MentionNeutralizer, text: str) -> str:
    return re.sub(r"(?<![A-Za-z0-9])@|&#0*64;?|&#[xX]0*40;?|&commat;?", "＠", text)


def _no_split(text: str) -> str:
    return text


def _no_forgectl(text: str) -> assemble.Neutralized:
    return assemble.Neutralized(text, 0, 0)


Patch = tuple[Any, str, Any]


def _mutations() -> list[tuple[str, list[Patch]]]:
    return [
        ("images not neutralized", [(render, "neutralize_characters", _no_image_pass)]),
        ("raw HTML not neutralized", [(render, "neutralize_characters", _no_lt_pass)]),
        (
            "images not neutralized in a body file",
            [(assemble, "neutralize_characters", _no_image_pass)],
        ),
        ("entity forms of @ not replaced", [(MentionNeutralizer, "neutralize", _literal_only)]),
        ("entity tails not seen", [(MentionNeutralizer, "neutralize", _no_entity_tail)]),
        ("suggestion fences not refused", [(textpipe, "suggestion_fence", lambda _t: False)]),
        (
            "a suggestion line fence not refused and not isolated from an open body fence",
            [
                (textsafety, "text_issues", lambda _d: []),
                (render, "_refuse_suggestion_fences", lambda _l: None),
                (render, "_isolate_run", lambda m: m.group()),
            ],
        ),
        ("a title is not escaped", [(render, "escape_title", lambda t: t)]),
        (
            "forgectl's neutralizing is not applied",
            [(assemble, "forgectl_neutralize", _no_forgectl)],
        ),
        ("a degraded suggestion is applicable", [(render, "degrade_reason", lambda _l: None)]),
    ]


@pytest.mark.parametrize("mutation", _mutations(), ids=[m[0] for m in _mutations()])
def test_the_fuzz_catches_a_broken_step(mutation: tuple[str, list[Patch]]) -> None:
    _, patches = mutation
    with pytest.MonkeyPatch.context() as patch:
        for owner, name, broken in patches:
            patch.setattr(owner, name, broken)
        with pytest.raises(pytest.fail.Exception):
            for seed in SEEDS:
                fuzz(seed, CASES // len(SEEDS))
