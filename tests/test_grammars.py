"""Value grammars for everything pr-comment forwards to forgectl (interface.md, forgectl edge)."""

from __future__ import annotations

import re

import pytest
from jsonschema import ValidationError

from pr_comment import grammars
from pr_comment.grammars import GrammarError
from tests.schema_harness import validate

SHA = "0123456789abcdef0123456789abcdef01234567"

# Values that must never be accepted by any grammar: flag injection, control characters, unicode
# lookalikes, whitespace and empties.
HOSTILE = [
    "",
    " ",
    "\n",
    "a\nb",
    "a\x00b",
    "a b",
    "a\tb",
    "a\u200bb",  # zero-width space
    "a\u202eb",  # bidi override
    "\uff1a",  # fullwidth colon
    "gh\uff1art\uff1aabc",
    "\U0001d7ce",  # mathematical digit zero
    "x" * 100_000,
]


def _accepts(check, value) -> bool:  # type: ignore[no-untyped-def]
    try:
        check(value)
    except GrammarError:
        return False
    return True


# --- PR argument ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://example.test/o/r/pull/1",
        "https://x",
        "example.test/o/r#7",
        "host/group/sub/project#123",
        "a#0",
    ],
)
def test_pr_valid(value: str) -> None:
    assert grammars.check_pr(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-x",
        "--yes",
        "-https://x",
        "-a#1",
        "https://",  # nothing after the scheme
        "http://example.test/o/r/pull/1",
        "example.test/o/r",  # no #N
        "example.test/o/r#",
        "example.test/o/r#1x",
        "example.test/o/r#x1",
        "#1",  # nothing before the #
        "example.test/o/r#1\n",
        "example.test/o/r#\u0661",  # Arabic-indic digit one
        "example.test/o/r\uff03" + "1",  # fullwidth number sign
        "https://example.test/o/r/pull/1 ",
        "https://example.test/o/r/pull/1\nsecond",
        "https://example.test/\x00",
        "https://example.test/\u202e",
        "https://example.test/\u00e9",
    ],
)
def test_pr_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_pr(value)


# --- commit shas ------------------------------------------------------------------------------


def test_sha_valid_lowercase_is_unchanged() -> None:
    assert grammars.check_sha(SHA) == SHA


def test_sha_uppercase_is_lowered() -> None:
    assert grammars.check_sha(SHA.upper()) == SHA


def test_sha_mixed_case_is_lowered() -> None:
    assert grammars.check_sha("AbCdEf" + SHA[6:]) == "abcdef" + SHA[6:]


@pytest.mark.parametrize(
    "value",
    [
        SHA[:39],  # one short
        SHA + "0",  # one long
        "a" * 64,  # sha-256 form is not supported
        "g" + SHA[1:],
        "-" + SHA[1:],
        "--" + SHA[2:],
        SHA + "\n",
        "\n" + SHA,
        SHA[:20] + "\x00" + SHA[21:],
        SHA[:-1] + "\uff10",  # fullwidth zero
        SHA[:-1] + "\U0001d7ce",
        "0x" + SHA[2:],
        "HEAD",
        "",
    ],
)
def test_sha_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_sha(value)


# --- branch names -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "main",
        "feat/x",
        "release/1.2.3",
        "user/feature_branch-2",
        "a",
        "refsx",  # only `refs` and `refs/` are refused
        "feat/refs/x",
        "caf\u00e9",
        "a" * 1024,
        "a.b",
        "x@y",  # a lone @ is fine, `@{` is not
    ],
)
def test_branch_valid(value: str) -> None:
    assert grammars.check_branch(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-main",
        "--yes",
        "/main",
        "main/",
        "a//b",
        "a..b",
        "..",
        "a@{b",
        "@{u}",
        "a b",
        "a\tb",
        "a\nb",
        "a\x00b",
        "a\x7fb",
        "a\x85b",  # C1 control
        "a~b",
        "a^b",
        "a:b",
        "a?b",
        "a*b",
        "a[b",
        "a\\b",
        "x.lock",
        "a.lock/b",
        "a.",
        ".hidden",
        "a/.hidden",
        "refs",
        "@",
        "refs/heads/main",
        "a\u202eb",  # bidi override
        "a\u200bb",  # zero-width space (format)
        "a\u2028b",  # line separator
        "a\u2029b",  # paragraph separator
        "a" * 1025,
    ],
)
def test_branch_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_branch(value)


# --- dedupe keys ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["a", "prc:rv:01ARZ3NDEKTSV4RRFFQ69G5FAV", "a.b_c-d:e", "k" * 128],
)
def test_dedupe_key_valid(value: str) -> None:
    assert grammars.check_dedupe_key(value) == value


def test_dedupe_key_leading_dash_is_in_the_grammar() -> None:
    # forgectl's grammar allows it and the value is forwarded as --dedupe-key=<k>, so it is
    # never read as an option.
    assert grammars.check_dedupe_key("-k") == "-k"


@pytest.mark.parametrize("value", ["k" * 129, "a b", "a/b", "a@b", "a\n", "k\u00e9", "\uff1a", ""])
def test_dedupe_key_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_dedupe_key(value)


# --- decision ids and actors ------------------------------------------------------------------


@pytest.mark.parametrize("value", ["d1", "a/b:c@d.e_f-g", "x" * 128, "01ARZ3", "-d"])
def test_decision_id_valid(value: str) -> None:
    assert grammars.check_decision_id(value) == value


@pytest.mark.parametrize("value", ["x" * 129, "a b", "a\n", "a#b", "\u00e9", ""])
def test_decision_id_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_decision_id(value)


@pytest.mark.parametrize("value", ["human:me", "auto:x", "auto:a.b_c@d-e", "human:" + "n" * 64])
def test_actor_valid(value: str) -> None:
    assert grammars.check_actor(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "human:",
        "human:" + "n" * 65,
        "robot:x",
        "Human:x",
        "human",
        "human:a b",
        "human:a\n",
        "human:a:b",
        "-human:x",
        "human:\u00e9",
        "",
    ],
)
def test_actor_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_actor(value)


# --- hook-supplied decision ids: a drop decision, never an exception --------------------------


@pytest.mark.parametrize("value", ["d1", "a/b:c@d", "x" * 128, "-d"])
def test_hook_decision_id_valid_is_kept(value: str) -> None:
    assert grammars.hook_decision_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "x" * 129,
        "a b",
        "",
        "a\n",
        "\u00e9",
        None,
        1,
        1.5,
        True,
        b"abc",
        ["a"],
        {"a": 1},
        object(),
        "x" * 1_000_000,
        "\ud800",  # lone surrogate
    ],
)
def test_hook_decision_id_invalid_is_dropped_never_raises(value: object) -> None:
    assert grammars.hook_decision_id(value) is None


# --- --expect-plan, --hook, --override-escalation ---------------------------------------------


def test_plan_hash_valid() -> None:
    assert grammars.check_plan_hash("a" * 64) == "a" * 64


@pytest.mark.parametrize("value", ["A" * 64, "a" * 63, "a" * 65, "a" * 63 + "\n", "g" * 64, ""])
def test_plan_hash_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_plan_hash(value)


def test_hook_valid() -> None:
    assert grammars.check_hook("pre-post=echo hi") == "pre-post=echo hi"
    assert grammars.check_hook("post-post=a=b") == "post-post=a=b"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "pre-post",
        "pre-post=",
        "other=cmd",
        "=cmd",
        "Pre-post=x",
        "pre-post=a\x00b",
        "pre-post=a\ud800b",
        "pre-post=a\udfffb",
        "pre-post=a\udcffb",
    ],
)
def test_hook_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_hook(value)


def test_escalation_scope() -> None:
    assert grammars.check_escalation_scope("pr-comment") == "pr-comment"
    assert grammars.check_escalation_scope("forgectl") == "forgectl"
    for bad in ("", "all", "FORGECTL", "forgectl ", "-x"):
        with pytest.raises(GrammarError):
            grammars.check_escalation_scope(bad)


# --- ids for --to and --thread ----------------------------------------------------------------

NODE = "PRRT_kwDOabc-_=9"
GL_D = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    "value",
    [
        "gh:ic:IC_kwDOabc",
        "gh:rc:" + NODE,
        "gh:rt:" + NODE,
        "gh:r:" + NODE,
        "gh:rt:" + "a" * 100,
        "gl:d:" + GL_D,
        "gl:n:1",
        "gl:n:123456",
    ],
)
def test_reply_target_valid(value: str) -> None:
    assert grammars.check_reply_target(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "gh:rt:" + "a" * 101,  # `gh:rt:` with 101 characters
        "gh:rt:",
        "gh:ic:",
        "gh:xx:abc",
        "gh:rt:a b",
        "gh:rt:a\n",
        "gh:rt:a:b",
        "gh:rt:\u00e9",
        "gh\uff1art\uff1aabc",
        "GH:rt:abc",
        "gl:d:" + GL_D[:39],  # `gl:d:` with 39 hex
        "gl:d:" + GL_D + "0",
        "gl:d:" + GL_D.upper(),
        "gl:d:" + "g" * 40,
        "gl:n:",
        "gl:n:x",
        "gl:n:-1",
        "gl:n:0",
        "gl:n:01",
        "gl:n:" + "1" * 20,
        "gl:n:1\n",
        "gl:n:\u0661",
        "gl:x:1",
        "-gh:rt:abc",
        "--yes",
        "abc",
        "",
    ],
)
def test_reply_target_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_reply_target(value)


@pytest.mark.parametrize("value", ["gh:rt:" + NODE, "gh:rt:" + "a" * 100, "gl:d:" + GL_D])
def test_thread_valid(value: str) -> None:
    assert grammars.check_thread(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "gh:ic:abc",  # well-formed reply targets that are not threads
        "gh:rc:abc",
        "gh:r:abc",
        "gl:n:1",
        "gh:rt:" + "a" * 101,
        "gl:d:" + GL_D[:39],
        "gh:rt:a\n",
        "-x",
        "",
    ],
)
def test_thread_invalid(value: str) -> None:
    with pytest.raises(GrammarError):
        grammars.check_thread(value)


# --- configured timeout and forgectl config path ----------------------------------------------


@pytest.mark.parametrize(
    "value", ["120s", "30s", "1ms", "2m", "1h", "7d", "0.5s", "10.25m", "30", "0.5", "5", "36500d"]
)
def test_timeout_valid_strings_are_returned_unchanged(value: str) -> None:
    assert grammars.check_timeout(value) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, "5"),
        (30, "30"),
        (0.5, "0.5"),
        (1e-05, "0.00001"),
        (3e9, "3000000000"),
        (1.5e9, "1500000000"),
        (3155760000, "3155760000"),
        (3155760000.0, "3155760000"),
    ],
)
def test_timeout_numbers_become_plain_decimal_strings(value: object, expected: str) -> None:
    result = grammars.check_timeout(value)
    assert result == expected
    assert re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", result)


@pytest.mark.parametrize(
    "value",
    [
        "-5s",
        "0s",
        "0",
        "0.0",
        "0.0s",
        "00",
        "5x",
        "s",
        "",
        "5 s",
        "5s\n",
        "\n5s",
        "--timeout=1s",
        "1e3s",
        ".5s",
        "5.s",
        "\u0665s",  # Arabic-indic digit five
        "5S",
        0,
        -5,
        -0.5,
        float("inf"),
        float("nan"),
        True,
        False,
        None,
        [],
        b"5s",
        # over forgectl's limit of 100 years (36,525 days), in every form
        "36526d",
        "3155760001",
        "3155760001s",
        "3155760000001ms",
        "52596001m",
        "876601h",
        "9" * 50 + "d",
        "9" * 50,
        "9" * 5000,
        "0." + "0" * 5000 + "1d",
        3155760001,
        3155760000.5,
        1e308,
        1e16,
        10**400,
        -(10**400),
    ],
)
def test_timeout_invalid(value: object) -> None:
    with pytest.raises(GrammarError):
        grammars.check_timeout(value)


@pytest.mark.parametrize("value", ["/etc/forgectl.yaml", "rel/path.yaml", "a", "./-x", "a b"])
def test_forgectl_config_valid(value: str) -> None:
    assert grammars.check_forgectl_config(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "-",
        "-x",
        "--config=x",
        "a\x00b",
        "\x00",
        "a\ud800b",
        "\udfff",
        "\udcff",
        None,
        5,
        ["a"],
        b"/x",
    ],
)
def test_forgectl_config_invalid(value: object) -> None:
    with pytest.raises(GrammarError):
        grammars.check_forgectl_config(value)


# --- cross-cutting: no grammar accepts hostile text -------------------------------------------

STRICT = [
    grammars.check_sha,
    grammars.check_dedupe_key,
    grammars.check_decision_id,
    grammars.check_actor,
    grammars.check_plan_hash,
    grammars.check_reply_target,
    grammars.check_thread,
    grammars.check_escalation_scope,
    grammars.check_hook,
    grammars.check_pr,
]
# Grammars whose character set can never start an option, or that refuse a leading dash itself.
DASH_REFUSING = [
    grammars.check_sha,
    grammars.check_actor,
    grammars.check_plan_hash,
    grammars.check_reply_target,
    grammars.check_thread,
    grammars.check_escalation_scope,
    grammars.check_hook,
    grammars.check_pr,
    grammars.check_branch,
]
DASH_VALUES = ["-", "-x", "--yes", "--yes=1", "-" + SHA[1:], "--config=/x"]


@pytest.mark.parametrize("check", STRICT, ids=lambda c: c.__name__)
@pytest.mark.parametrize("value", HOSTILE, ids=lambda v: repr(v)[:30])
def test_no_grammar_accepts_hostile_text(check, value: str) -> None:  # type: ignore[no-untyped-def]
    assert not _accepts(check, value)


def test_grammar_errors_never_echo_the_value() -> None:
    secret = "-secret-token-value"
    for check in STRICT:
        try:
            check(secret)
        except GrammarError as exc:
            assert "secret" not in str(exc)


@pytest.mark.parametrize("check", DASH_REFUSING, ids=lambda c: c.__name__)
@pytest.mark.parametrize("value", DASH_VALUES)
def test_leading_dash_is_refused(check, value: str) -> None:  # type: ignore[no-untyped-def]
    assert not _accepts(check, value)


@pytest.mark.parametrize(
    "value", [v for v in HOSTILE if "\uff1a" not in v and "\U0001d7ce" not in v]
)
def test_branch_refuses_hostile_text(value: str) -> None:
    assert not _accepts(grammars.check_branch, value)


# --- agreement with schemas/defs.json ---------------------------------------------------------

AGREEMENT: list[tuple[str, object, list[str]]] = [
    ("id", grammars.check_reply_target, ["gh:rt:abc", "gl:n:5", "gl:n:0", "gh:ic:", "gh:x:a"]),
    ("thread_id", grammars.check_thread, ["gh:rt:abc", "gl:d:" + GL_D, "gl:n:5", "gh:r:a"]),
    ("dedupe_key", grammars.check_dedupe_key, ["a:b", "a b", "k" * 129, "-x"]),
    ("decision_id", grammars.check_decision_id, ["a/b", "a b", "x" * 129, "-x"]),
    ("actor", grammars.check_actor, ["human:a", "auto:", "bot:a", "human:a b"]),
    ("sha256", grammars.check_plan_hash, ["a" * 64, "A" * 64, "a" * 63]),
    ("base_ref", grammars.check_branch, ["main", "a..b", "a@{b", "a//b", "x.lock", "-x", "a b"]),
]


@pytest.mark.parametrize(("name", "check", "values"), AGREEMENT, ids=[a[0] for a in AGREEMENT])
def test_a_grammar_never_accepts_what_the_schema_rejects(
    name: str,
    check,
    values: list[str],  # type: ignore[no-untyped-def]
) -> None:
    for value in values:
        if _accepts(check, value):
            validate(f"pr-comment/defs.json#/$defs/{name}", value)


@pytest.mark.parametrize(
    ("name", "check", "values"), AGREEMENT[:-1], ids=[a[0] for a in AGREEMENT[:-1]]
)
def test_a_grammar_accepts_what_the_schema_accepts(
    name: str,
    check,
    values: list[str],  # type: ignore[no-untyped-def]
) -> None:
    for value in values:
        try:
            validate(f"pr-comment/defs.json#/$defs/{name}", value)
        except ValidationError:
            continue
        assert _accepts(check, value), value


@pytest.mark.parametrize(
    "value", ["@", ".hidden", "a/.hidden", "a/b/.c", "a" * 1025, "a..b", "a/", "x.lock", "-x"]
)
def test_the_schema_base_ref_refuses_what_check_branch_refuses(value: str) -> None:
    assert not _accepts(grammars.check_branch, value)
    with pytest.raises(ValidationError):
        validate("pr-comment/defs.json#/$defs/base_ref", value)


@pytest.mark.parametrize("value", ["main", "feat/x", "a.b", "x@y", "a" * 1024, "a/b.c"])
def test_the_schema_base_ref_accepts_valid_branches(value: str) -> None:
    validate("pr-comment/defs.json#/$defs/base_ref", value)


# --- --since (export, issue #38) ------------------------------------------------------------


SINCE_VALID = [
    "7d",
    "1s",
    "90m",
    "36h",
    "2026-11-17T10:00:00Z",
    "2026-11-17t10:00:00z",
    "2026-11-17T10:00:00.123456789Z",
    "2026-11-17T10:00:00.1234567890123Z",  # any number of digits
    "2026-11-17T10:00:00+05:30",
    "2026-11-17T10:00:00-00:00",
    "2999-01-01T00:00:00Z",
]
SINCE_INVALID = [
    "",
    " 7d",
    "7d ",
    "7d\n",
    "7",
    "0d",
    "00s",
    "-7d",
    "+7d",
    "7.5d",
    "7D",
    "7ms",
    "7w",
    "d",
    "7 d",
    "７d",  # fullwidth digit
    "1" * 40 + "s",
    "999999999d",  # over 100 years
    "2026-11-17",
    "2026-11-17T10:00:00",  # no offset
    "2026-11-17 10:00:00Z",
    "2026-13-17T10:00:00Z",
    "2026-02-30T10:00:00Z",
    "2026-11-17T24:00:00Z",
    "2026-11-17T10:00:60Z",  # leap second: refused (narrower)
    "2026-11-17T10:00:00+24:00",
    "2026-11-17T10:00:00+0530",
    "2026-11-17T10:00:00.Z",
    "0000-01-01T00:00:00Z",
    "0001-01-01T00:00:00+23:59",  # before year 1 once converted to UTC
    "2026-11-17T10:00:00Z\n",
    "2026-11-17T10:00:00Z; rm -rf",
    "--since",
    "\x00",
]


@pytest.mark.parametrize("value", SINCE_VALID)
def test_check_since_accepts_rfc3339_with_an_offset_and_durations(value: str) -> None:
    assert grammars.check_since(value) == value


@pytest.mark.parametrize("value", SINCE_INVALID)
def test_check_since_refuses_everything_else_without_echoing_it(value: str) -> None:
    with pytest.raises(GrammarError) as raised:
        grammars.check_since(value)
    if len(value.strip()) > 12:  # shorter ones can equal the message's own examples
        assert value not in str(raised.value)


def test_check_since_refuses_a_non_string() -> None:
    for value in (None, 7, b"7d", ["7d"]):
        with pytest.raises(GrammarError):
            grammars.check_since(value)


def test_since_cutoff_of_a_duration_counts_back_from_now() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert grammars.since_cutoff("7d", now) == now - timedelta(days=7)
    assert grammars.since_cutoff("90m", now) == now - timedelta(minutes=90)
    assert grammars.since_cutoff("5s", now) == now - timedelta(seconds=5)


def test_since_cutoff_of_a_timestamp_honors_the_offset() -> None:
    from datetime import UTC, datetime

    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert grammars.since_cutoff("2026-11-17T10:00:00+02:00", now) == datetime(
        2026, 11, 17, 8, 0, tzinfo=UTC
    )
    assert grammars.since_cutoff("2026-11-17T10:00:00.5Z", now) == datetime(
        2026, 11, 17, 10, 0, 0, 500000, tzinfo=UTC
    )


def test_parse_rfc3339_returns_none_for_anything_check_since_would_refuse_as_a_time() -> None:
    assert grammars.parse_rfc3339("2026-11-17T10:00:00Z") is not None
    for value in ("7d", "2026-11-17T10:00:00", "", "2026-11-17T10:00:00Z\n"):
        assert grammars.parse_rfc3339(value) is None
