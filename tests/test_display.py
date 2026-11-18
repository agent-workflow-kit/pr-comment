"""Display sanitizing: forgectl's Sanitizing rules for untrusted text shown on a terminal.

Expected values are written out by hand from forgectl's interface (Sanitizing section), not
derived from the implementation's own tables.
"""

import sys
import unicodedata

import pytest

from pr_comment.display import (
    count_marked,
    is_marked,
    marker,
    sanitize_bytes,
    sanitize_line,
    sanitize_text,
)

# forgectl: "all variation selectors, U+034F, U+17B4, U+17B5, U+180B to U+180F, U+115F, U+1160,
# U+3164, U+FFA0 and U+2800", spelled out one by one.
LISTED_INVISIBLE = [
    *range(0xFE00, 0xFE10),
    *range(0xE0100, 0xE01F0),
    0x034F,
    0x17B4,
    0x17B5,
    0x180B,
    0x180C,
    0x180D,
    0x180E,
    0x180F,
    0x115F,
    0x1160,
    0x3164,
    0xFFA0,
    0x2800,
]

# One or two stable representatives per Unicode category (none of them is a listed code point).
MARKED_BY_CATEGORY = {
    "Cc": [0x00, 0x01, 0x07, 0x08, 0x0B, 0x0C, 0x0D, 0x1B, 0x1F, 0x7F, 0x80, 0x85, 0x9B, 0x9F],
    "Cf": [0x00AD, 0x200B, 0x200D, 0x200E, 0x202A, 0x202E, 0x2066, 0x2069, 0xFEFF, 0xE0001],
    "Cs": [0xD800, 0xDBFF, 0xDC00, 0xDFFF],
    "Co": [0xE000, 0xF8FF, 0xF0000, 0x10FFFD],
    "Cn": [0x0378, 0xFFFF, 0x10FFFF, 0xFDD0],
    "Zl": [0x2028],
    "Zp": [0x2029],
}

UNMARKED_BY_CATEGORY = {
    "Lu": "A",
    "Ll": "z",
    "Lt": "\u01c5",
    "Lm": "\u02b0",
    "Lo": "\u4e2d",
    "Mn": "\u0301",
    "Mc": "\u0903",
    "Me": "\u20dd",
    "Nd": "7",
    "Nl": "\u2160",
    "No": "\u00b2",
    "Pc": "_",
    "Pd": "-",
    "Ps": "(",
    "Pe": ")",
    "Pi": "\u00ab",
    "Pf": "\u00bb",
    "Po": "!",
    "Sm": "+",
    "Sc": "$",
    "Sk": "^",
    "So": "\U0001f600",
    "Zs": " ",
    "NBSP": "\u00a0",
    "IDEOGRAPHIC SPACE": "\u3000",
    "FFFD": "\ufffd",
}


def test_marker_format() -> None:
    assert marker("\u202e") == "<U+202E>"
    assert marker("\x00") == "<U+0000>"
    assert marker("\x1b") == "<U+001B>"
    assert marker("\U000e0100") == "<U+E0100>"
    assert marker("\U0010ffff") == "<U+10FFFF>"
    assert marker("\ud800") == "<U+D800>"


@pytest.mark.parametrize(
    ("category", "code"),
    [(cat, code) for cat, codes in MARKED_BY_CATEGORY.items() for code in codes],
)
def test_marked_categories(category: str, code: int) -> None:
    ch = chr(code)
    # Tab and newline are the only Cc exceptions, and they are not in this table.
    assert is_marked(ch)
    assert sanitize_text(ch) == f"<U+{code:04X}>"
    assert sanitize_line(ch) == f"<U+{code:04X}>"


@pytest.mark.parametrize("code", LISTED_INVISIBLE)
def test_listed_invisible_code_points_are_marked(code: int) -> None:
    assert is_marked(chr(code))
    assert sanitize_text(chr(code)) == f"<U+{code:04X}>"


@pytest.mark.parametrize("name", list(UNMARKED_BY_CATEGORY))
def test_unmarked_categories_pass_through(name: str) -> None:
    ch = UNMARKED_BY_CATEGORY[name]
    assert not is_marked(ch)
    assert sanitize_text(ch) == ch
    assert sanitize_line(ch) == ch


def test_neighbours_of_listed_ranges_are_not_marked() -> None:
    # Off-by-one guards: just outside each listed range, and similar looking characters.
    for code in (0xFDFF, 0xFE10, 0xE00FF, 0xE01F0, 0x034E, 0x0350, 0x17B3, 0x17B6, 0x1810):
        ch = chr(code)
        expect_marked = unicodedata.category(ch) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
        assert is_marked(ch) is expect_marked, hex(code)
    assert not is_marked("\u115e")
    assert not is_marked("\u1161")
    assert not is_marked("\u3165")
    assert not is_marked("\uff9f")
    assert not is_marked("\u2801")
    assert not is_marked("\u27ff")


def test_tab_and_lf_pass_in_text_but_not_in_one_line_fields() -> None:
    assert not is_marked("\t")
    assert not is_marked("\n")
    assert sanitize_text("a\tb\nc") == "a\tb\nc"
    assert sanitize_line("a\tb\nc") == "a<U+0009>b<U+000A>c"


def test_crlf_becomes_lf_and_lone_cr_is_marked_in_text() -> None:
    assert sanitize_text("a\r\nb") == "a\nb"
    assert sanitize_text("a\rb") == "a<U+000D>b"
    assert sanitize_text("a\r\r\nb") == "a<U+000D>\nb"
    assert sanitize_text("a\n\rb") == "a\n<U+000D>b"
    assert sanitize_text("a\r") == "a<U+000D>"


def test_cr_is_always_marked_in_one_line_fields() -> None:
    assert sanitize_line("a\r\nb") == "a<U+000D><U+000A>b"
    assert sanitize_line("a\rb") == "a<U+000D>b"


def test_esc_sequences_render_as_markers() -> None:
    hostile = "\x1b[31mred\x1b[0m \x1b]0;pwned\x07 \x1bP1$r\x1b\\ \x9b31m \x9d0;t\x9c \x90q\x9c"
    out = sanitize_text(hostile)
    for code in (0x1B, 0x07, 0x9B, 0x9D, 0x9C, 0x90):
        assert chr(code) not in out
    assert out.startswith("<U+001B>[31mred<U+001B>[0m ")
    assert "<U+001B>]0;pwned<U+0007>" in out
    assert "<U+001B>P1$r<U+001B>\\" in out
    assert "<U+009B>31m" in out
    assert "<U+009D>0;t<U+009C>" in out
    assert "<U+0090>q<U+009C>" in out


def test_bidi_override_renders_as_marker() -> None:
    out = sanitize_text("harmless\u202etxt.exe")
    assert out == "harmless<U+202E>txt.exe"
    assert "\u202e" not in out
    for ch in "\u202a\u202b\u202c\u202d\u2066\u2067\u2068\u2069\u200e\u200f\u061c":
        assert chr(ord(ch)) not in sanitize_text(f"x{ch}y")


def test_lone_surrogates_do_not_raise_and_are_marked() -> None:
    out = sanitize_text("a\ud800b\udfffc")
    assert out == "a<U+D800>b<U+DFFF>c"
    # The result is encodable: nothing unprintable survives.
    out.encode("utf-8")


def test_paired_surrogate_escapes_are_not_joined() -> None:
    # Two lone surrogates that would form an astral pair in UTF-16 stay two markers.
    assert sanitize_text(chr(0xD83D) + chr(0xDE00)) == "<U+D83D><U+DE00>"


def test_astral_characters_pass_through() -> None:
    assert (
        sanitize_text("ok \U0001f600 \U0001f1fa\U0001f1f8") == "ok \U0001f600 \U0001f1fa\U0001f1f8"
    )


def test_marker_text_in_input_is_not_special() -> None:
    # A literal marker typed by an attacker is left as is: display never decodes markers.
    assert sanitize_text("<U+202E>") == "<U+202E>"


def test_empty_and_plain() -> None:
    assert sanitize_text("") == ""
    assert sanitize_line("") == ""
    assert sanitize_text("hello world") == "hello world"


def test_huge_input_is_linear_and_complete() -> None:
    text = "\x1b[0m" * 200_000
    out = sanitize_text(text)
    assert out == "<U+001B>[0m" * 200_000


def test_sanitizing_is_idempotent() -> None:
    hostile = "a\x1b[1m\u202e\ud800\u200d\ufe0f\u2800\u0378 \r\n\t"
    once = sanitize_text(hostile)
    assert sanitize_text(once) == once
    one_line = sanitize_line(hostile)
    assert sanitize_line(one_line) == one_line


def test_every_code_point_matches_the_documented_rule() -> None:
    listed = set(LISTED_INVISIBLE)
    marked_categories = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
    unicode_pinned = unicodedata.unidata_version
    for code in range(sys.maxunicode + 1):
        ch = chr(code)
        expect = (
            code in listed
            or unicodedata.category(ch) in marked_categories
            and code not in (0x09, 0x0A)
        )
        assert is_marked(ch) is expect, (hex(code), unicode_pinned)
        line_out = sanitize_line(ch)
        if code in (0x09, 0x0A) or expect:
            assert line_out == f"<U+{code:04X}>", hex(code)
        else:
            assert line_out == ch, hex(code)


def test_output_has_no_terminal_active_characters_for_any_code_point() -> None:
    everything = "".join(chr(c) for c in range(sys.maxunicode + 1))
    out = sanitize_line(everything)
    for ch in out:
        assert unicodedata.category(ch) not in {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}, hex(ord(ch))


def test_count_marked_counts_each_character() -> None:
    assert count_marked("") == 0
    assert count_marked("plain \u00e9\U0001f600 \t\n") == 0
    assert count_marked("a\x1b[0m\u202e\u200b\ud800\ufe0f") == 5
    assert count_marked("\u2800\u2800") == 2


def test_count_marked_agrees_with_sanitize_text_markers() -> None:
    text = "x\x00\x1b\u202e\u200d\ud800\u2028\ue000\u0378\u3164 y\t\n"
    assert count_marked(text) == sanitize_text(text).count("<U+")


def test_sanitize_bytes_replaces_invalid_utf8_with_fffd() -> None:
    assert sanitize_bytes(b"ok") == "ok"
    out = sanitize_bytes(b"a\xffb\xc3\x28c\xed\xa0\x80d")
    assert "\ufffd" in out
    assert out.startswith("a\ufffdb")
    assert out.endswith("d")
    assert "<U+D800>" not in out  # encoded surrogates are invalid UTF-8, not surrogates


def test_sanitize_bytes_truncated_sequence_and_overlong() -> None:
    assert "\ufffd" in sanitize_bytes(b"abc\xe2\x82")
    assert "\ufffd" in sanitize_bytes(b"\xc0\xaf")
    assert sanitize_bytes(b"") == ""


def test_sanitize_bytes_marks_controls_and_applies_line_mode() -> None:
    assert sanitize_bytes(b"a\x1b[0m\r\nb") == "a<U+001B>[0m\nb"
    assert sanitize_bytes(b"a\tb\n", one_line=True) == "a<U+0009>b<U+000A>"
    assert sanitize_bytes("\u202e".encode()) == "<U+202E>"


def test_sanitize_bytes_c1_as_utf8_is_marked() -> None:
    # U+009B as UTF-8 (C2 9B) is a C1 control and is marked; a raw 0x9B byte is invalid UTF-8.
    assert sanitize_bytes(b"\xc2\x9b31m") == "<U+009B>31m"
    assert sanitize_bytes(b"\x9b31m") == "\ufffd31m"


def test_display_does_not_change_its_input_or_json_text() -> None:
    # Display sanitizing returns new text; the posted text (and --json output) stays raw.
    raw = "a\x1b[0m\u202e"
    sanitize_text(raw)
    assert raw == "a\x1b[0m\u202e"
