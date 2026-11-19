"""``counts.links`` is an upper bound on the links a reader can get (issue #86).

Every inline link or image destination ``](``, every link reference definition, every angle
autolink and every bare form is counted. The differential tests render with the pinned cmarkgfm
(GitHub's engine) and require the count to cover every ``<a>`` and ``<img>`` it produces. A
reference use (a closing bracket not followed by ``(`` or ``:``) and every ``[^`` is counted too,
so a definition used several times, and a footnote (a back-reference per use), stay covered.
"""

from __future__ import annotations

import random
import re
import time

import pytest

from pr_comment.neutralize import count_text
from tests.textsafety import render_footnotes, render_safe

_TAG_RE = re.compile(r"<(?:a|img)[ >]")


def _rendered_links(markdown: str) -> int:
    return len(_TAG_RE.findall(render_safe(markdown)))


def _links(text: str) -> int:
    return count_text(text).links


@pytest.mark.parametrize(
    ("text", "links"),
    [
        ("[x](u)", 1),
        ("[x](/relative/path)", 1),
        ("[x](javascript:alert(1))", 1),
        ("[x]( javascript:alert(1))", 1),
        ("![x](u)", 1),
        ("[x](a) [y](b)", 2),
        ("[x][r]\n\n[r]: u", 3),
        ("[x]: javascript:x", 1),
        ("[x]: <u> 'title'", 1),
        ("   [x]: u", 1),
        ('[x]: u\n"title"', 1),
        ("[a]: u\n[b]: v", 2),
        ("[x]\n\n[x]: /u", 2),
        ("[x] [x]\n\n[x]: u", 3),
        ("a[^1]\n\n[^1]: n", 4),
        ("> [x]: u", 1),
        ("<javascript:alert(1)>", 1),
        ("<foo:bar>", 1),
        ("<a+b-c.d:e>", 1),
        ("<HTTP:x>", 1),
        ("<mailto:a@b.co>", 3),
        ("&#91;x&#93;&#40;u&#41;", 1),
        ("[x]\\(u)", 1),
        ("plain [brackets] and (parens)", 1),
        ("plain brackets and (parens)", 0),
        ("< a:b>", 0),
        ("<1a:b>", 0),
    ],
)
def test_link_forms(text: str, links: int) -> None:
    assert _links(text) == links


@pytest.mark.parametrize(
    "text",
    [
        "[x](javascript:alert(1))",
        "[x](u 'title')",
        "[x](<u v>)",
        "![x](u)",
        "![x][r]\n\n[r]: u",
        "[x][]\n\n[x]: u",
        "[x]\n\n[x]: u",
        "[x][r]\n\n   [r]: u 'title'",
        "[a [b](u) c](v)",
        "<https://example.org>",
        "<javascript:alert(1)>",
        "<irc:foo>",
        "https://example.org/a",
        "www.example.org",
        "a@b.co",
        "mailto:a@b.co",
        "[x]: u\n[x]",
        "[multi\nline]: u\n\n[multi line]",
        "> [x]\n>\n> [x]: u",
        "- [x][r]\n\n[r]: u",
    ],
)
def test_golden_cases_are_covered_by_the_count(text: str) -> None:
    assert _rendered_links(text) >= 1, "the case must render a link"
    assert _links(text) >= _rendered_links(text), text


def test_a_reference_used_twice_is_counted_per_use() -> None:
    text = "[x] [x]\n\n[x]: u"
    assert _rendered_links(text) == 2
    assert _links(text) >= 2


def test_a_footnote_is_counted_for_both_its_anchors() -> None:
    text = "a[^1]\n\n[^1]: n"
    rendered = len(_TAG_RE.findall(render_footnotes(text)))
    assert rendered == 2
    assert _links(text) >= rendered


def test_a_footnote_used_twice_is_covered() -> None:
    text = "a[^f0] a[^f0]  **[t](u)**\n\n[^f0]: note"
    rendered = len(_TAG_RE.findall(render_footnotes(text)))
    assert rendered == 5
    assert _links(text) >= rendered


_FRAGMENTS = [
    "[t](u)",
    "[t](javascript:alert(1))",
    "[t](u 'ti')",
    "![t](u)",
    "[t]()",
    "[t](<a b>)",
    "<https://e.org>",
    "<x:y>",
    "<a@b.co>",
    "https://e.org",
    "www.e.org",
    "a@b.co",
    "mailto:a@b.co",
    "`[t](u)`",
    "```\n[t](u)\n```",
    "\\[t](u)",
    "[t]\\(u)",
    "**[t](u)**",
    "> [t](u)",
    "- [t](u)",
    "&#91;t](u)",
    "[a [b](u)](v)",
    "[ ] task",
    "plain words",
    "\n",
    "\n\n",
    "  ",
]


def _fuzz_document(rng: random.Random, footnotes: bool = False) -> str:
    out: list[str] = []
    defs: list[str] = []
    for _ in range(rng.randint(1, 8)):
        kind = rng.randint(0, 5)
        if kind == 0:
            label = f"r{len(defs)}"
            uses = [f"[t][{label}]", f"![t][{label}]", f"[{label}][]", f"[{label}]"]
            out.extend(rng.choice(uses) + " " for _ in range(rng.randint(1, 3)))
            defs.append(
                rng.choice(
                    [
                        f"[{label}]: u",
                        f"   [{label}]: <u v> 'title'",
                        f"[{label}]: javascript:x",
                        f'[{label}]: u\n"title"',
                        f"[{label}]:\n  u",
                    ]
                )
            )
        elif footnotes and kind == 1:
            label = f"^f{len(defs)}"
            out.extend(f"a[{label}] " for _ in range(rng.randint(1, 3)))
            defs.append(f"[{label}]: note")
        else:
            out.append(rng.choice(_FRAGMENTS))
        out.append(rng.choice([" ", "\n", "\n\n"]))
    return "".join(out) + "\n\n" + "\n".join(defs)


def test_differential_fuzz_against_cmarkgfm() -> None:
    rng = random.Random(86)
    rendered_any = 0
    for _ in range(1500):
        text = _fuzz_document(rng)
        rendered = _rendered_links(text)
        rendered_any += rendered > 0
        assert _links(text) >= rendered, text
    assert rendered_any > 500


def test_differential_fuzz_with_footnotes_rendering_too() -> None:
    rng = random.Random(861)
    for _ in range(300):
        text = _fuzz_document(rng, footnotes=True)
        rendered = len(_TAG_RE.findall(render_footnotes(text)))
        assert _links(text) >= rendered, text


# --- hostile input: linear time -------------------------------------------------------------

_ADVERSARIAL = [
    "](" * 500_000,
    "[" * 1_000_000,
    "]" * 1_000_000,
    "]:" * 500_000,
    "<" + "a" * 999_999,
    "<" + "a" * 40 + "<" * 500_000,
    "<a:" * 333_333,
    "\n   [" * 200_000,
    "[" + "x" * 999_990 + "]: u",
    "a" * 500_000 + "@" + "b" * 499_000,
    "&#" + "9" * 999_000,
    "&" * 1_000_000,
    "\\" * 1_000_000,
    " " * 1_000_000 + "[x]: u",
]


@pytest.mark.parametrize("index", range(len(_ADVERSARIAL)))
def test_counting_is_linear_on_adversarial_input(index: int) -> None:
    text = _ADVERSARIAL[index]
    start = time.perf_counter()
    count_text(text)
    assert time.perf_counter() - start < 5.0


def test_counting_time_grows_linearly() -> None:
    def cost(n: int) -> float:
        text = "<a:[x](u) \n  [r]: v " * n
        best = float("inf")
        for _ in range(3):
            start = time.perf_counter()
            count_text(text)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = cost(20_000), cost(160_000)
    assert large < small * 8 * 3 + 0.05  # 8x the input, generous slack for noise
