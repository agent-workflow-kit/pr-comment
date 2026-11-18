"""Mention neutralizing (docs/interface.md: Mentions) and the --allow-mention value grammar.

The inputs are hostile on purpose: every spelling of `@` that a host decodes, names with invisible
characters, mentions in every Markdown context, allowlist near-misses, and very large texts. The
decoded-text oracle and the cmark-gfm oracle both ask the same question the host asks: does any
`@` survive that a host could turn into a mention?
"""

from __future__ import annotations

import html
import json
import random
import re
import time
from pathlib import Path

import cmarkgfm  # pyright: ignore[reportMissingTypeStubs]
import pytest

from pr_comment import grammars
from pr_comment.grammars import GrammarError
from pr_comment.mentions import MentionNeutralizer

AT = "＠"  # FULLWIDTH COMMERCIAL AT


def run(text: str, allow: tuple[str, ...] = ()) -> tuple[str, tuple[str, ...], int]:
    neutralizer = MentionNeutralizer("neutralize", allow)
    out = neutralizer.neutralize(text)
    return out, neutralizer.names, neutralizer.count


# --- what is replaced -------------------------------------------------------------------------

# (input, output, reported names, replaced forms), no allowlist.
REPLACED = [
    ("@user", f"{AT}user", ("user",), 1),
    ("hi @user, and @user2.", f"hi {AT}user, and {AT}user2.", ("user", "user2"), 2),
    ("@org/team", f"{AT}org/team", ("org/team",), 1),
    ("@group/sub/proj", f"{AT}group/sub/proj", ("group/sub/proj",), 1),
    ("@", AT, (), 1),
    ("@ x", f"{AT} x", (), 1),
    ("@.", f"{AT}.", (), 1),
    ("@.x", f"{AT}.x", (".x",), 1),
    ("@-x", f"{AT}-x", ("-x",), 1),
    ("@@x", f"{AT}{AT}x", ("x",), 2),
    ("\\@x", f"\\{AT}x", ("x",), 1),
    ("a`@x", f"a`{AT}x", ("x",), 1),
    ("`@x`", f"`{AT}x`", ("x",), 1),
    ("@&#111;ctocat", f"{AT}&#111;ctocat", (), 1),
    ("@\\_name", f"{AT}\\_name", (), 1),
    ("&#64victim", f"{AT}victim", ("victim",), 1),
    ("&#64;victim", f"{AT}victim", ("victim",), 1),
    ("&#064;victim", f"{AT}victim", ("victim",), 1),
    ("&#0000000064;victim", f"{AT}victim", ("victim",), 1),
    ("&#x40;victim", f"{AT}victim", ("victim",), 1),
    ("&#X40;victim", f"{AT}victim", ("victim",), 1),
    ("&#x0040victim", f"{AT}victim", ("victim",), 1),
    ("&#x40 victim", f"{AT} victim", (), 1),
    ("&commat;victim", f"{AT}victim", ("victim",), 1),
    ("&commat victim", f"{AT} victim", (), 1),
    ("x&#64y", f"x{AT}y", ("y",), 1),  # an entity form is replaced wherever it stands
    ("é@x", f"é{AT}x", ("x",), 1),
    ("_@x", f"_{AT}x", ("x",), 1),
    (".@x", f".{AT}x", ("x",), 1),
    ("-@x", f"-{AT}x", ("x",), 1),
    ("​@x", f"​{AT}x", ("x",), 1),  # a zero-width character is not a letter
    ("a​@x", f"a​{AT}x", ("x",), 1),
    ("a­@x", f"a­{AT}x", ("x",), 1),
    ("а@x", f"а{AT}x", ("x",), 1),  # a Cyrillic letter is not an ASCII letter
    ("ａ@x", f"ａ{AT}x", ("x",), 1),  # fullwidth a
    ("\n@x", f"\n{AT}x", ("x",), 1),
    ("x\n@y", f"x\n{AT}y", ("y",), 1),
    ("@ALL @here @channel", f"{AT}ALL {AT}here {AT}channel", ("all", "here", "channel"), 3),
    ("@User @user @USER", f"{AT}User {AT}user {AT}USER", ("user",), 3),
    ("@a @b @a", f"{AT}a {AT}b {AT}a", ("a", "b"), 3),
    # the preceding character is judged on the output, so a replaced form is not a letter
    ("a&#64@x", f"a{AT}{AT}x", ("x",), 2),
    ("x@@y", f"x@{AT}y", ("y",), 1),
    # a name stops at the first character outside its class
    ("@jo​hn", f"{AT}jo​hn", ("jo",), 1),
    ("@john​", f"{AT}john​", ("john",), 1),
    ("@jöhn", f"{AT}jöhn", ("j",), 1),
    ("@john＠x", f"{AT}john{AT}x", ("john",), 1),
    ("@a/b/c/", f"{AT}a/b/c/", ("a/b/c",), 1),
    ("@a//b", f"{AT}a//b", ("a",), 1),
    ("@a.b.", f"{AT}a.b.", ("a.b",), 1),
    ("@a...", f"{AT}a...", ("a",), 1),
    ("@...", f"{AT}...", (), 1),
    ("@org/team.", f"{AT}org/team.", ("org/team",), 1),
    ("@a/.", f"{AT}a/.", ("a",), 1),  # the trailing dot, then the slash it exposed, are removed
]


@pytest.mark.parametrize(("text", "out", "names", "count"), REPLACED)
def test_replaced_forms(text: str, out: str, names: tuple[str, ...], count: int) -> None:
    assert run(text) == (out, names, count)


# Left alone: nothing a host reads as a mention, nothing counted or reported.
UNCHANGED = [
    "",
    "no mention here",
    "a@b",
    "a@b.com",
    "user.name+tag@example.test",
    "1@x",
    "Z@x",
    f"{AT}user",  # already fullwidth
    "&#640;",  # &#640 is another character
    "&#x400;",
    "&#x40a;",
    "&#x40F;",
    "&#6;",
    "&#6 4;",
    "&amp;#64;",
    "&amp;commat;",
    "&#38;#64;",
    "&com;",
    "&COMMAT;",
]


@pytest.mark.parametrize("text", UNCHANGED)
def test_unchanged_text(text: str) -> None:
    assert run(text) == (text, (), 0)


# An `@` after an unterminated entity-like tail (`&amp`, `&#38`, `&#x26`) is replaced: a downstream
# HTML parser decodes the tail to a non-word character and the `@` would be live.
ENTITY_TAIL_CASES = [
    ("<div>&amp@octocat</div>", f"<div>&amp{AT}octocat</div>"),
    ("<div>&#38@octocat", f"<div>&#38{AT}octocat"),
    ("<p>&lt@john", f"<p>&lt{AT}john"),
    ("<!--x-->&#x4@name", f"<!--x-->&#x4{AT}name"),
    ("&#X26@a", f"&#X26{AT}a"),
    ("&copy@a", f"&copy{AT}a"),
    ("&#xa@a", f"&#xa{AT}a"),
    ("R&D@corp", f"R&D{AT}corp"),
]


@pytest.mark.parametrize(("text", "out"), ENTITY_TAIL_CASES)
def test_an_at_after_an_unterminated_entity_tail_is_replaced(text: str, out: str) -> None:
    assert run(text)[0] == out
    assert run(text)[2] == 1


@pytest.mark.parametrize("text", ["a@b.com", "x&y;z@b", "a#38@b", "&#x@b", "&#3x@b"])
def test_an_at_not_after_an_entity_tail_stays_email_like(text: str) -> None:
    assert run(text) == (text, (), 0)


def test_an_email_and_a_terminated_reference_keep_their_old_behavior() -> None:
    assert run("a@b.com") == ("a@b.com", (), 0)
    # `;` is not a letter or digit, so the `@` was replaced before and still is.
    assert run("&amp;@x") == (f"&amp;{AT}x", ("x",), 1)


def test_an_allowed_name_is_kept_after_an_entity_tail() -> None:
    assert run("&amp@john", ("john",)) == ("&amp@john", (), 0)


def test_a_bare_commat_without_semicolon_is_replaced() -> None:
    assert run("&commat") == (AT, (), 1)


def test_empty_names_are_counted_but_not_listed() -> None:
    assert run("@ @ @&#64;") == (f"{AT} {AT} {AT}{AT}", (), 4)


# --- contexts: code, quotes, lists, tables, links, quick actions --------------------------------

CONTEXTS = [
    "`@x`",
    "``@x``",
    "```\n@x\n```",
    "~~~\n@x\n~~~",
    "    @x",
    "\t@x",
    "> @x",
    "> > @x",
    ">>> \n@x\n>>>",
    "- @x",
    "1. @x",
    "- > @x",
    "| a |\n|---|\n| @x |",
    "[@x](https://example.test)",
    "[a](https://example.test/@x)",
    "<https://example.test/@x>",
    "https://example.test/@x",
    "![@x](https://example.test/a.png)",
    "(@x)",
    '"@x"',
    "'@x'",
    "*@x*",
    "**@x**",
    "~~@x~~",
    "_@x_",
    "# @x",
    "text.@x",
    "text,@x",
    "text:@x",
    "/assign @x",
    "/cc @x @y",
    "/assign_reviewer @x",
    "[a]: /u\n    @x",
    "foo\\\n@x",
    "&#64x",
    "\\&#64x",
    "@x\\@y",
    "&commat;x",
    "@y/z",
    "@y/z/w",
    "@all",
    "@here",
    "@channel",
    "　@x",  # ideographic space
    " @x",  # no-break space
    "‮@x",
    "﻿@x",
    "\U0001f600@x",
]


@pytest.mark.parametrize("text", CONTEXTS)
def test_no_live_mention_survives_in_any_context(text: str) -> None:
    out, _, count = run(text)
    assert count >= 1
    assert not LIVE.search(out)
    assert not LIVE.search(html.unescape(out))


# An `@` a host could read as a mention (docs/interface.md: a non-letter before it), literal or
# decoded.
LIVE = re.compile(r"(?<![A-Za-z0-9])@")


def _cmark_text(markdown: str) -> str:
    """The text nodes of GitHub's Markdown rendering (the tags dropped)."""
    page = cmarkgfm.github_flavored_markdown_to_html(markdown)  # pyright: ignore[reportUnknownMemberType]
    return html.unescape(re.sub(r"<[^>]*>", "", page))


def test_the_oracle_can_fail() -> None:
    # Without neutralizing, the very same inputs leak in cmark-gfm's output.
    for text in ["@x", "`@x`", "&#64;x", "\\@x", "> - @x"]:
        assert LIVE.search(_cmark_text(text)), text


@pytest.mark.parametrize("text", CONTEXTS)
def test_cmark_gfm_renders_no_live_mention(text: str) -> None:
    out, _, _ = run(text)
    assert not LIVE.search(_cmark_text(out)), text


def test_the_divergence_cases_of_the_review_passes() -> None:
    cases = [
        "[a]: /u\n    @victim",
        "| a |\n|---|\n| b |\n|\n    @victim",
        "> x\n\t> ```\n@victim",
        "~~~\n@victim\n",
        "```\n@victim\n````\n@victim",
    ]
    for text in cases:
        out, _, _ = run(text)
        assert not LIVE.search(_cmark_text(out)), text


TOKENS = [
    "@",
    "@x",
    "@org/t",
    "&#64;",
    "&#x40",
    "&#x40;",
    "&commat;",
    "a",
    "1",
    "\\",
    "`",
    "``",
    "```",
    "~~~",
    ">",
    "- ",
    "1. ",
    "|",
    "|---|",
    "[a]: /u",
    "    ",
    "\t",
    "\n",
    "\n\n",
    " ",
    "*",
    "_",
    "(",
    ")",
    "[",
    "](",
    ".",
    ",",
    "​",
    "＠",
    "é",
]


def test_differential_fuzz_against_cmark_gfm() -> None:
    rng = random.Random(16)
    for _ in range(3000):
        text = "".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 30)))
        out, _, _ = run(text)
        assert not LIVE.search(_cmark_text(out)), repr(text)


def test_decoded_text_fuzz_with_terminated_entities() -> None:
    # html.unescape decodes an entity without its `;` too, which cmark-gfm does not: the oracle
    # is exact only for terminated references (`&#x40a@x` is "J" + `@x` there, not a mention).
    rng = random.Random(18)
    tokens = [t for t in TOKENS if not t.startswith("&#x40") or t.endswith(";")] + ["&#x40a;"]
    for _ in range(3000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 30)))
        out, _, _ = run(text)
        assert not LIVE.search(html.unescape(out)), repr(text)


def test_html5_decoded_fuzz_with_unterminated_entity_tails() -> None:
    # html.unescape decodes `&amp`, `&#38` and `&#x26` without their `;` as an HTML5 parser does:
    # no `@` may be live after decoding, whatever entity-like tail precedes it.
    rng = random.Random(19)
    tokens = ["@", "@x", "a", "1", "&", "&#", "&#x", "&amp", "&lt", "&copy", "&#38", "&#x26"]
    tokens += ["&#x4", "&#xa", ";", " ", "<p>", "&#64;", "&commat;"]
    for _ in range(5000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(1, 12)))
        out, _, _ = run(text)
        assert not LIVE.search(html.unescape(out)), repr(text)


def test_neutralizing_is_idempotent() -> None:
    rng = random.Random(17)
    for _ in range(500):
        text = "".join(rng.choice(TOKENS) for _ in range(rng.randint(1, 30)))
        once, _, _ = run(text)
        assert run(once)[0] == once, repr(text)


# --- the allowlist -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "@john",
        "@john ",
        "@john\n",
        "@john\t",
        "@john,",
        "@john;",
        "@john:",
        "@john!",
        "@john?",
        "@john)",
        "@john]",
        "@john}",
        "@john'",
        '@john"',
        "@john*",
        "@john~",
        "@john.",
        "@john...",
        "@john. next",
        "@john.\nnext",
        "@john.. next",
        "hi @john",
        "(@john)",
        "@john @john",
    ],
)
def test_an_allowed_name_with_an_allowed_follow_is_kept(text: str) -> None:
    assert run(text, ("john",)) == (text, (), 0)


@pytest.mark.parametrize(
    ("text", "name"),
    [
        ("@john.doe", "john.doe"),
        ("@john.d", "john.d"),
        ("@john_", "john_"),
        ("@john-", "john-"),
        ("@john2", "john2"),
        ("@john/", "john"),
        ("@john/x", "john/x"),
        ("@john\\", "john"),
        ("@john<", "john"),
        ("@john&#33;", "john"),
        ("@john&amp;", "john"),
        ("@john​", "john"),
        ("@john ", "john"),  # a no-break space is not whitespace here
        ("@john　", "john"),
        ("@johń", "john"),
        ("@john`", "john"),
        ("@john>", "john"),
        ("@john|", "john"),
        ("@john[", "john"),
        ("@john(", "john"),
        ("@john{", "john"),
        ("@john@", "john"),
        ("@john＠", "john"),
        ("@john.​", "john"),
        ("@john.x", "john.x"),
        ("@john./", "john"),
        ("@john.,", "john"),  # one or more dots, then whitespace or the end: not a comma
        ("@john.)", "john"),
        ("@john.*", "john"),
        ("@jonh", "jonh"),
        ("@joh", "joh"),
        ("@johnny", "johnny"),
        ("@a/john", "a/john"),
    ],
)
def test_an_allowed_name_with_another_follow_or_a_longer_name_is_replaced(
    text: str, name: str
) -> None:
    out, names, count = run(text, ("john",))
    assert out == text.replace("@", AT, 1)
    assert names == (name,)
    assert count == 1


def test_matching_ignores_ascii_case_on_both_sides() -> None:
    assert run("@John @JOHN @jOhN", ("JOHN",)) == ("@John @JOHN @jOhN", (), 0)
    assert run("@john", ("John",)) == ("@john", (), 0)


def test_matching_is_ascii_only() -> None:
    # U+0130 and the Kelvin sign lower to ASCII letters under Unicode rules, not under ASCII's.
    assert run("@K", ("k",)) == (f"{AT}K", (), 1)
    with pytest.raises(ValueError, match="allow"):  # a non-ASCII entry is outside the grammar
        run("@k", ("\u212a",))


def test_a_team_entry_covers_exactly_that_team() -> None:
    allow = ("org/team",)
    assert run("@org/team", allow) == ("@org/team", (), 0)
    assert run("@ORG/Team, hi", allow) == ("@ORG/Team, hi", (), 0)
    assert run("@org/team/sub", allow) == (f"{AT}org/team/sub", ("org/team/sub",), 1)
    assert run("@org", allow) == (f"{AT}org", ("org",), 1)
    assert run("@org/", allow) == (f"{AT}org/", ("org",), 1)
    assert run("@org/team.", allow) == ("@org/team.", (), 0)
    assert run("@org/team./", allow) == (f"{AT}org/team./", ("org/team",), 1)
    assert run("@org/teams", allow) == (f"{AT}org/teams", ("org/teams",), 1)
    assert run("@x/org/team", allow) == (f"{AT}x/org/team", ("x/org/team",), 1)


def test_a_name_entry_does_not_cover_a_team_or_a_dotted_name() -> None:
    assert run("@org/team", ("org",)) == (f"{AT}org/team", ("org/team",), 1)
    assert run("@john.doe", ("john",)) == (f"{AT}john.doe", ("john.doe",), 1)


def test_the_allowlist_never_covers_an_entity_form() -> None:
    for text in ["&#64john", "&#64;john", "&#x40;john", "&commat;john"]:
        assert run(text, ("john",)) == (f"{AT}john", ("john",), 1), text


def test_an_allowed_name_overrides_the_preceding_character_rule() -> None:
    # The allowlist decides for every literal `@` that would be replaced, so a name the user
    # allowed is kept after `_` too. `a@john` is e-mail-like and untouched either way.
    assert run("a@john", ("john",)) == ("a@john", (), 0)
    assert run("_@john", ("john",)) == ("_@john", (), 0)
    assert run("&#64@john", ("john",)) == (f"{AT}@john", (), 1)


@pytest.mark.parametrize("entry", ["@john", "a b", "zz/", "/zz", "a//b", "é", "a\nb", "＠x"])
def test_an_allowlist_entry_outside_the_name_grammar_is_refused(entry: str) -> None:
    with pytest.raises(ValueError, match="allow"):
        MentionNeutralizer("neutralize", (entry,))


def test_an_empty_allowlist_entry_is_refused_with_a_message() -> None:
    with pytest.raises(ValueError, match="allow") as excinfo:
        MentionNeutralizer("neutralize", ("",))
    assert str(excinfo.value)


@pytest.mark.parametrize(
    "entry", ["zzmarker@", "@johnmarker", "zzmarker&#64;", "zz marker\nzzmarker"]
)
def test_a_refused_allowlist_entry_is_not_echoed(entry: str) -> None:
    with pytest.raises(ValueError, match="allow") as excinfo:
        MentionNeutralizer("neutralize", (entry,))
    message = str(excinfo.value)
    assert entry not in message
    assert "zzmarker" not in message
    assert "johnmarker" not in message


def test_the_name_grammar_is_defined_once() -> None:
    from pr_comment import mentions

    assert mentions._NAME_RE is grammars.MENTION_NAME_RE  # pyright: ignore[reportPrivateUsage]
    schema = json.loads((Path(__file__).parent.parent / "schemas" / "defs.json").read_text())
    pattern = schema["$defs"]["mention_name"]["pattern"]
    assert pattern == f"^{grammars.MENTION_NAME_RE.pattern}$"


def test_an_entry_with_a_trailing_dot_never_matches() -> None:
    assert run("@john", ("john.",)) == (f"{AT}john", ("john",), 1)
    assert run("@john.", ("john.",)) == (f"{AT}john.", ("john",), 1)


def test_allowed_and_replaced_mix_in_one_text() -> None:
    out, names, count = run("@john and @eve and @JOHN.", ("john",))
    assert (out, names, count) == (f"@john and {AT}eve and @JOHN.", ("eve",), 1)


# --- policy -----------------------------------------------------------------------------------


def test_policy_allow_turns_the_pass_off() -> None:
    neutralizer = MentionNeutralizer("allow", ())
    text = "@x &#64y \\@z"
    assert neutralizer.neutralize(text) == text
    assert neutralizer.names == ()
    assert neutralizer.count == 0


def test_an_unknown_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="policy"):
        MentionNeutralizer("deny", ())  # pyright: ignore[reportArgumentType]


def test_the_default_policy_is_neutralize() -> None:
    assert MentionNeutralizer().neutralize("@x") == f"{AT}x"


# --- reporting across parts --------------------------------------------------------------------


def test_names_accumulate_across_parts_in_first_appearance_order_without_repeats() -> None:
    neutralizer = MentionNeutralizer()
    neutralizer.neutralize("@Bob and @amy")
    neutralizer.neutralize("@AMY @carol")
    neutralizer.neutralize("@bob")
    assert neutralizer.names == ("bob", "amy", "carol")
    assert neutralizer.count == 5


def test_a_part_boundary_counts_as_a_non_letter() -> None:
    neutralizer = MentionNeutralizer()
    neutralizer.neutralize("a")
    assert neutralizer.neutralize("@x") == f"{AT}x"
    assert neutralizer.names == ("x",)


def test_a_reported_name_is_cut_to_128_characters_before_dedupe() -> None:
    neutralizer = MentionNeutralizer()
    long = "a" * 128
    out = neutralizer.neutralize(f"@{long}b @{long}c @{'a' * 500}")
    assert neutralizer.names == (long,)
    assert neutralizer.count == 3
    assert out == f"{AT}{long}b {AT}{long}c {AT}{'a' * 500}"  # the text is neutralized exactly


def test_a_cut_that_ends_in_a_slash_drops_the_slash_so_the_name_fits_the_schema() -> None:
    neutralizer = MentionNeutralizer()
    head = "a" * 127
    out = neutralizer.neutralize(f"@{head}/b @{head}/c")
    assert neutralizer.names == (head,)
    assert neutralizer.count == 2
    assert out == f"{AT}{head}/b {AT}{head}/c"
    pattern = json.loads(Path("schemas/defs.json").read_text())["$defs"]["mention_name_reported"]
    assert re.fullmatch(pattern["pattern"], neutralizer.names[0])


def test_at_most_100_names_are_reported_but_every_form_is_counted() -> None:
    neutralizer = MentionNeutralizer()
    neutralizer.neutralize(" ".join(f"@u{i}" for i in range(150)))
    assert neutralizer.names == tuple(f"u{i}" for i in range(100))
    assert neutralizer.count == 150
    neutralizer.neutralize("@u0 @late")
    assert neutralizer.names == tuple(f"u{i}" for i in range(100))
    assert neutralizer.count == 152


def test_the_names_cap_does_not_change_the_neutralized_text() -> None:
    text = " ".join(f"@u{i}" for i in range(150))
    out = MentionNeutralizer().neutralize(text)
    assert "@" not in out


def test_reported_names_hold_only_name_grammar_characters() -> None:
    neutralizer = MentionNeutralizer()
    hostile = "@a‮b @c\x1b[31md @e\nf @g<script>h @i​j @`k @аl @m n"
    neutralizer.neutralize(hostile)
    assert neutralizer.names
    for name in neutralizer.names:
        assert re.fullmatch(r"[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*", name), name


def test_the_attribution_line_with_an_actor_named_after_a_broadcast() -> None:
    # The footer renderer (#20) writes the actor as a code span; neutralizing covers code spans.
    line = "Posted by an automated agent `auto:@all`, approved by `human:@here`."
    out, names, count = run(line)
    assert out == f"Posted by an automated agent `auto:{AT}all`, approved by `human:{AT}here`."
    assert names == ("all", "here")
    assert count == 2


# --- size and speed ----------------------------------------------------------------------------

BIG = 2_000_000


@pytest.mark.parametrize(
    "text",
    [
        "@" * BIG,
        "@a" * (BIG // 2),
        "@a." * (BIG // 3),
        "@a/" * (BIG // 3),
        "&#" + "0" * BIG,
        "&#0" * (BIG // 3),
        "&#x" + "0" * BIG,
        "&#x0" * (BIG // 4),
        "&#" + "0" * BIG + "64",
        "&#" + "0" * BIG + "640",
        "&commat" * (BIG // 7),
        "&" * BIG,
        "a" * BIG + "@x",
        "@" + "a" * BIG,
        "@" + "a/" * (BIG // 2),
        "@" + "a." * (BIG // 2) + " ",
        "@" + "." * BIG,
        "&#64" * (BIG // 4),
        " @" * (BIG // 2),
        "\\@" * (BIG // 2),
        "@​" * (BIG // 2),
    ],
    ids=lambda t: f"{t[:8]!r}x{len(t)}",
)
def test_huge_hostile_inputs_run_in_bounded_time(text: str) -> None:
    started = time.monotonic()
    neutralizer = MentionNeutralizer()
    out = neutralizer.neutralize(text)
    assert time.monotonic() - started < 10
    assert not LIVE.search(out)
    assert len(neutralizer.names) <= len(text)


def test_many_distinct_names_run_in_bounded_time() -> None:
    text = " ".join(f"@u{i}" for i in range(200_000))
    started = time.monotonic()
    neutralizer = MentionNeutralizer()
    neutralizer.neutralize(text)
    assert time.monotonic() - started < 10
    assert len(neutralizer.names) == 100
    assert neutralizer.count == 200_000


# --- the --allow-mention value grammar ---------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["john", "John", "a", "a.b", "a-b_c", "org/team", "g/sub/proj", "1", ".x", "-x", "a."]
)
def test_mention_name_valid_is_returned_unchanged(value: str) -> None:
    assert grammars.check_mention_name(value) == value


INVALID_NAMES = [
    "",
    "@john",
    "@org/team",
    "john ",
    " john",
    "john\n",
    "a b",
    "a\tb",
    "/a",
    "a/",
    "a//b",
    "a/b/",
    "é",
    "a​b",
    "＠john",
    "john＠",
    "а",
    "a\x00b",
    "a&#64;",
    "a,b",
    "a\\b",
    "a`b",
    "a<b",
    "*",
    "\U0001d7ce",
    # distinctive values, so "the value is absent from the message" cannot hold by accident
    "a\nzzmarker",
    "zzmarker@",
    "@johnmarker",
    "zzmarker&#64;",
]


@pytest.mark.parametrize("value", INVALID_NAMES)
def test_mention_name_invalid(value: str) -> None:
    with pytest.raises(GrammarError) as excinfo:
        grammars.check_mention_name(value)
    message = str(excinfo.value)
    assert "mention name" in message
    if value:
        assert value not in message
        assert "zzmarker" not in message
        assert "johnmarker" not in message


def test_mention_name_refuses_a_non_string() -> None:
    for value in (None, 1, b"a", ["a"]):
        with pytest.raises(GrammarError):
            grammars.check_mention_name(value)


def test_the_flag_grammar_and_the_matcher_agree_on_what_a_name_is() -> None:
    # Every grammar-valid entry that does not end in a dot is kept when it appears alone.
    for entry in ["john", "a.b", "org/team", "g/sub/proj", "-x", "a_b"]:
        assert run(f"@{entry}", (grammars.check_mention_name(entry),)) == (f"@{entry}", (), 0)
