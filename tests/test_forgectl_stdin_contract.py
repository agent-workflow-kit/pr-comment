"""The stdin contract between pr-comment and the pinned forgectl.

pr-comment sends untrusted text (a review file, a comment, a reply or an approval body) to forgectl
only on stdin, after ``--file -`` or ``--body-file -``, never in argv or the environment
(docs/interface.md, forgectl edge). These tests run pr-comment's own ``ProcessGateway`` over the
recorded cases of ``forgectl_stdin_cases.json`` (and the gateway-expressible post cases of
``forgectl_cases.json``), twice:

- against the pinned fake (always): the fake must end every case as the recording says, and every
  call it receives must keep the edge rules (``assert_edge_rules``);
- against the real pinned forgectl (``live`` marker, never in CI): the same cases, so a change in
  forgectl that the fake did not follow fails here. The real binary runs in a scratch HOME with no
  credentials and a ``PATH`` holding one empty directory (no ``gh`` or ``glab``), so every case
  ends before a host is read (a refusal, a dry-run plan, or ``tool_missing``); nothing is ever
  written to a host.

The cases cover the four commands that take input, in ``--dry-run`` and ``--yes`` modes: valid
input, empty, large, CRLF, NUL, non-UTF-8 and lone-surrogate bytes, text that looks like flags,
the size boundaries, forgectl refusing on its command line before it reads stdin (the gateway then
ends with forgectl's exit status, not a pipe error), and the closed argv forms.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from pr_comment import gateway as gateway_module
from pr_comment.gateway import (
    ForgectlResult,
    GatewayUsageError,
    ProcessGateway,
    validate_call,
)
from tests.forgectl_cases import (
    CASES_FILE,
    STDIN_CASES_FILE,
    error_code,
    expected_of,
    load,
    observed_of,
    stdin_bytes,
)
from tests.forgectl_harness import (
    PINNED_VERSION,
    FakeForgectl,
    assert_edge_rules,
    fail,
    ok,
    whoami_doc,
)

POST_COMMANDS = ("review", "comment", "reply", "approve")
# Read once at import: the autouse ``private_home`` fixture removes every ``PR_COMMENT_*`` variable
# from the environment of a test. Set it in CI (or any run that must prove the contract) so a
# missing or other-version forgectl fails the live tests instead of skipping them.
REQUIRE_FORGECTL = bool(os.environ.get("PR_COMMENT_REQUIRE_FORGECTL"))
PIPE_BUFFER = 1 << 20  # input larger than this (the pipe holds 64 KiB) must be read in full
STDIN_RECORDED = load(STDIN_CASES_FILE)
STDIN_CASES: list[dict[str, Any]] = STDIN_RECORDED["cases"]
MAIN_CASES: list[dict[str, Any]] = [
    c
    for c in load(CASES_FILE)["cases"]
    if c["argv"][0] in POST_COMMANDS and "{tmp}" not in " ".join(c["argv"])
]


def split(case: dict[str, Any]) -> tuple[str, list[str], bytes | None]:
    """The gateway call of a case: subcommand, arguments (the gateway adds ``--json``) and stdin
    (given exactly when the arguments carry the stdin ``-``)."""
    subcommand, *rest = case["argv"]
    args = [a for a in rest if a != "--json"]
    data = stdin_bytes(case["stdin"])
    return subcommand, args, data if "-" in args else None


def refused_by_gateway(case: dict[str, Any]) -> bool:
    subcommand, args, data = split(case)
    try:
        validate_call(subcommand, args, data)
    except GatewayUsageError:
        return True
    return False


# Main cases the gateway can express (the others are the closed forms, checked below).
EXPRESSIBLE = [c for c in STDIN_CASES + MAIN_CASES if not refused_by_gateway(c)]
CLOSED = [c for c in STDIN_CASES + MAIN_CASES if refused_by_gateway(c)]


def ids(cases: list[dict[str, Any]]) -> list[str]:
    return [c["name"] for c in cases]


def body_texts(data: bytes | None) -> list[str]:
    """The untrusted text of a case, when it is text long enough not to match flag names."""
    if not data:
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return [text] if len(text) >= 4 else []


class PopenSpy:
    """Records what the gateway hands to ``subprocess.Popen`` (argv, environment, stdin kind) and
    starts the process as usual, so the same check holds for the fake and the real binary."""

    def __init__(self, real: Callable[..., Any]) -> None:
        self.real = real
        self.starts: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.starts.append(
            {
                "argv": list(argv),
                "env": dict(kwargs.get("env") or {}),
                "stdin": kwargs.get("stdin"),  # the raw kwarg: None is not DEVNULL
                "cwd": kwargs.get("cwd"),
                "shell": kwargs.get("shell"),
            }
        )
        return self.real(argv, **kwargs)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> PopenSpy:
    s = PopenSpy(subprocess.Popen)
    monkeypatch.setattr(gateway_module.subprocess, "Popen", s)
    return s


def assert_text_only_on_stdin(spy: PopenSpy, case: dict[str, Any], data: bytes | None) -> None:
    """No body text in argv or the environment; stdin is a pipe exactly when the call carries ``-``;
    no shell; working directory ``/``."""
    assert len(spy.starts) == 1
    start = spy.starts[0]
    assert start["shell"] is False and start["cwd"] == "/"
    if "-" in start["argv"]:
        assert start["stdin"] is subprocess.PIPE, case["name"]
    else:
        assert start["stdin"] is subprocess.DEVNULL, case["name"]
    assert (start["stdin"] is subprocess.PIPE) == (data is not None)
    for text in body_texts(data):
        assert not any(text in arg for arg in start["argv"]), case["name"]
        assert not any(text in k or text in v for k, v in start["env"].items()), case["name"]
    assert not any(k.startswith("PR_COMMENT_") for k in start["env"])


def assert_sigpipe_ignored() -> None:
    """The early-exit pass relies on Python's default: SIGPIPE ignored, so a write to a closed pipe
    is an EPIPE the gateway handles. A reset to SIG_DFL would turn the exit status into a signal
    death."""
    assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN, (
        "SIGPIPE is not ignored: a write to forgectl's closed stdin would kill the process with "
        "a signal instead of ending with forgectl's exit status"
    )


def check_result(case: dict[str, Any], result: ForgectlResult, data: bytes | None = None) -> None:
    """The gateway ended with forgectl's own exit status and output, as recorded."""
    assert result.timed_out is False and result.leftover is False
    assert result.signal is None
    if "reaches_host" in case:
        assert case["expect"] == {"reaches_host": True}
        assert result.exit_code == 12 and error_code(result.stdout) == "tool_missing", result.stdout
    else:
        assert observed_of(result.exit_code, result.stdout, result.stderr) == expected_of(case)
    if case.get("stdin_unread"):
        # forgectl ended without taking its (over 8 MiB) stdin: not a pipe error, its exit status
        assert_sigpipe_ignored()
        assert result.stdin_unread is True
    elif data is not None and len(data) > PIPE_BUFFER:
        # a large input forgectl must read in full (the 16 MiB review cases): never "unread"
        assert result.stdin_unread is False, case["name"]


# --- the pinned fake ---------------------------------------------------------------------------


def fake_gateway(fake: FakeForgectl) -> ProcessGateway:
    return ProcessGateway(forgectl_bin="forgectl", environ=fake.environ(), deadline=120)


def script_host_missing(fake: FakeForgectl, case: dict[str, Any]) -> None:
    """What the real forgectl met after its input checks, with no ``gh`` or ``glab`` on its
    ``PATH``."""
    if "reaches_host" in case:
        tool = "glab" if any(a.startswith("gitlab.com/") for a in case["argv"]) else "gh"
        install = f"brew install {tool}"
        fake.script(
            fail(
                case["argv"][0],
                "tool_missing",
                f"{tool} was not found",
                details={"install": install},
            )
        )


@pytest.mark.parametrize("case", EXPRESSIBLE, ids=ids(EXPRESSIBLE))
def test_the_fake_ends_every_stdin_case_as_the_real_forgectl_did(
    case: dict[str, Any], forgectl: FakeForgectl, spy: PopenSpy, tmp_path: Path
) -> None:
    forgectl.allow_usage_errors = True  # the cases that forgectl refuses are the point
    forgectl.allow_confirmation_blocked = True  # a write without --yes stops at its prompt
    script_host_missing(forgectl, case)
    subcommand, args, data = split(case)
    result = fake_gateway(forgectl).call(subcommand, args, stdin=data)
    check_result(case, result, data)
    assert_text_only_on_stdin(spy, case, data)
    calls = forgectl.calls()
    assert len(calls) == 1
    # Every edge rule over what the fake saw, for the calls pr-comment could make: the recorded
    # main cases that forgectl refuses on a value (an empty --dedupe-key, say) are values the
    # caller's own grammars stop before the gateway, so only the accepted ones are held to the
    # rules. The confirmed input is the stdin byte for byte when forgectl read it all (a call it
    # refused first may have read none or part of it).
    if case in STDIN_CASES or "reaches_host" in case:
        assert_edge_rules(
            calls,
            texts=body_texts(data),
            confirmed="--yes" in args,
            root=tmp_path,
            confirmed_input=data if "reaches_host" in case and data is not None else None,
        )


@pytest.mark.parametrize("case", CLOSED, ids=ids(CLOSED))
def test_the_gateway_refuses_the_closed_forms_before_it_starts_forgectl(
    case: dict[str, Any], forgectl: FakeForgectl, spy: PopenSpy
) -> None:
    subcommand, args, data = split(case)
    with pytest.raises(GatewayUsageError):
        fake_gateway(forgectl).call(subcommand, args, stdin=data)
    assert spy.starts == [] and forgectl.calls() == []


@pytest.mark.parametrize("case", CLOSED, ids=ids(CLOSED))
def test_the_fake_ends_the_closed_forms_as_the_real_forgectl_did(
    case: dict[str, Any], forgectl: FakeForgectl
) -> None:
    """The gateway never sends these, but forgectl's answer to them is recorded all the same, so
    a form the fake accepted and forgectl refuses (or the reverse) shows up."""
    forgectl.allow_usage_errors = True
    forgectl.allow_confirmation_blocked = True
    script_host_missing(forgectl, case)
    proc = forgectl.run(case["argv"], stdin=stdin_bytes(case["stdin"]))
    if "reaches_host" in case:
        assert proc.returncode == 12 and error_code(proc.stdout) == "tool_missing"
    else:
        assert observed_of(proc.returncode, proc.stdout, proc.stderr) == expected_of(case)


def test_the_recorded_closed_forms_are_the_ones_the_gateway_refuses() -> None:
    flagged = {c["name"] for c in STDIN_CASES if c.get("gateway_refuses")}
    assert flagged == {c["name"] for c in STDIN_CASES if refused_by_gateway(c)}
    assert len(flagged) >= 8


def test_the_recording_is_of_the_pinned_release_and_covers_every_post_command() -> None:
    pin = json.loads((Path(__file__).resolve().parent / "forgectl_pin.json").read_text())
    assert STDIN_RECORDED["_meta"]["recorded_from"] == f"forgectl {pin['version']}"
    assert PINNED_VERSION == pin["version"]
    assert len({c["name"] for c in STDIN_CASES}) == len(STDIN_CASES)
    for command in POST_COMMANDS:
        for mode in ("dry-run", "yes"):
            wanted = f"stdin-{command}-{mode}-"
            names = [c["name"] for c in STDIN_CASES if c["name"].startswith(wanted)]
            for kind in ("valid", "empty", "crlf", "nul", "lone-surrogate-bytes", "all-bytes"):
                assert f"{wanted}{kind}" in names
            # what forgectl accepts, as well as what it refuses, is recorded
            assert any("reaches_host" in c for c in STDIN_CASES if c["name"] in names)


def test_the_recording_file_is_byte_stable_under_its_dump_options() -> None:
    """Regenerating the file (see ``_meta.how``) and dumping it with the same options reproduces
    it byte for byte, so a hand edit or a different dump shows up as a diff."""
    text = STDIN_CASES_FILE.read_text(encoding="utf-8")
    assert json.dumps(json.loads(text), indent=1, ensure_ascii=False) + "\n" == text


def test_every_post_command_is_recorded_accepting_the_pre_check_flag_set() -> None:
    """The dry run the pre-check sends carries the pins, ``--actor``, ``--dedupe-key`` and
    ``--decision-id`` (docs/interface.md); forgectl accepts it up to the host for each command."""
    wanted = {"--actor=auto:x", "--dedupe-key=k", "--decision-id=d", "--dry-run"}
    for command in POST_COMMANDS:
        pins = (
            {"--expected-head=", "--expected-base="} if command in ("review", "approve") else set()
        )
        assert any(
            "reaches_host" in c
            and c["argv"][0] == command
            and wanted <= set(c["argv"])
            and all(any(a.startswith(p) for a in c["argv"]) for p in pins)
            and ("--file" in c["argv"] or "--body-file" in c["argv"])
            for c in STDIN_CASES
        ), command


def test_gitlab_host_cases_are_recorded_for_comment_and_review() -> None:
    gitlab = [c for c in STDIN_CASES if any(a.startswith("gitlab.com/") for a in c["argv"])]
    names = {c["name"] for c in gitlab}
    assert {
        "stdin-comment-yes-gitlab-valid",
        "stdin-comment-yes-gitlab-at-limit",
        "stdin-comment-yes-gitlab-over-limit",
        "stdin-review-yes-gitlab-valid",
    } <= names
    assert all(("reaches_host" in c) == ("over-limit" not in c["name"]) for c in gitlab)


def test_every_post_command_is_recorded_accepting_stdin_in_both_modes() -> None:
    for command in POST_COMMANDS:
        for flag in ("--dry-run", "--yes"):
            assert any(
                "reaches_host" in c
                and c["argv"][0] == command
                and flag in c["argv"]
                and ("--file" in c["argv"] or "--body-file" in c["argv"])
                for c in STDIN_CASES
            ), (command, flag)


def test_recorded_over_8_mib_cases_include_a_refusal_before_stdin_is_read() -> None:
    unread = [c for c in STDIN_CASES if c.get("stdin_unread")]
    commands = {c["argv"][0] for c in unread}
    assert commands == set(POST_COMMANDS)
    assert all(len(stdin_bytes(c["stdin"])) >= 8 << 20 for c in unread)


def test_the_fake_closing_stdin_early_ends_with_its_exit_status(
    forgectl: FakeForgectl, spy: PopenSpy
) -> None:
    """A forgectl that refuses on its command line never reads 32 MiB: the gateway still ends with
    forgectl's exit status and envelope, with no pipe error and no hang."""
    forgectl.allow_usage_errors = True
    data = b"x" * (32 << 20)
    assert_sigpipe_ignored()
    result = fake_gateway(forgectl).call(
        "comment",
        ["github.com/o/r#1", "--body-file", "-", "--yes", "--actor=bogus"],
        stdin=data,
    )
    assert result.exit_code == 2 and error_code(result.stdout) == "usage"
    assert result.stdin_unread is True and result.timed_out is False
    assert forgectl.calls()[0].stdin in (None, b"")  # it never read the body


def test_a_call_without_stdin_starts_forgectl_with_devnull(
    forgectl: FakeForgectl, spy: PopenSpy
) -> None:
    """The raw ``stdin`` kwarg of a read-style call is ``subprocess.DEVNULL``, not ``None`` (which
    would hand forgectl the caller's stdin)."""
    forgectl.script(ok("whoami", whoami_doc(["github.com"])))
    fake_gateway(forgectl).call("whoami", ["--host=github.com"])
    assert len(spy.starts) == 1
    assert spy.starts[0]["stdin"] is subprocess.DEVNULL


def test_a_missing_forgectl_skips_unless_it_is_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "REQUIRE_FORGECTL", False)
    with pytest.raises(pytest.skip.Exception):
        real_forgectl(tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "REQUIRE_FORGECTL", True)
    with pytest.raises(pytest.fail.Exception):
        real_forgectl(tmp_path)


def test_an_untrusted_text_never_reaches_argv_or_the_environment_even_if_it_looks_like_flags(
    forgectl: FakeForgectl, spy: PopenSpy, tmp_path: Path
) -> None:
    canary = b"--yes --actor=human:x $(touch /nonexistent) `id` ; PR_COMMENT_X=1 \x00 \xed\xa0\x80"
    forgectl.allow_usage_errors = True
    for subcommand, args in (
        ("comment", ["github.com/o/r#1", "--body-file", "-", "--yes"]),
        ("reply", ["github.com/o/r#1", "--to=gh:rt:T_1", "--body-file", "-", "--dry-run"]),
        ("approve", ["github.com/o/r#1", "--body-file", "-", "--dry-run"]),
        ("review", ["github.com/o/r#1", "--file", "-", "--dry-run"]),
    ):
        fake_gateway(forgectl).call(subcommand, args, stdin=canary)
    for start in spy.starts:
        joined = b"\x00".join(a.encode() for a in start["argv"]) + b"\x00".join(
            f"{k}={v}".encode() for k, v in start["env"].items()
        )
        assert b"$(touch" not in joined and b"PR_COMMENT_X" not in joined
        assert b"human:x" not in joined and b"`id`" not in joined
    for call in forgectl.calls():
        assert call.stdin in (None, canary) or canary.startswith(call.stdin or b"")


# --- the real pinned forgectl (live) -------------------------------------------------------------


def unavailable(reason: str) -> NoReturn:
    """Skip the live run, or fail it when ``PR_COMMENT_REQUIRE_FORGECTL`` is set."""
    if REQUIRE_FORGECTL:
        pytest.fail(f"live stdin contract (required): {reason}")
    pytest.skip(f"live stdin contract: {reason}")


def real_forgectl(root: Path) -> str:
    """The absolute path of the pinned forgectl; its ``--version`` runs in the scratch environment
    too, never with the real HOME or ``PATH``."""
    found = shutil.which("forgectl")
    if found is None:
        unavailable("no forgectl on PATH (install the pinned release)")
    found = str(Path(found).resolve())
    version = subprocess.run(
        [found, "--version"],
        capture_output=True,
        text=True,
        check=True,
        env=scratch_environment(root),
        cwd="/",
        timeout=60,
    )
    if version.stdout.strip() != f"forgectl {PINNED_VERSION}":
        unavailable(f"forgectl is {version.stdout.strip()!r}, the pin is {PINNED_VERSION}")
    return found


def scratch_environment(root: Path) -> dict[str, str]:
    """HOME and every XDG directory private, no credential variable, and a ``PATH`` of one empty
    absolute directory: no ``gh`` or ``glab``, so nothing can reach a host."""
    home = root / "home"
    empty = root / "empty-bin"
    home.mkdir(parents=True, exist_ok=True)
    empty.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_DATA_HOME": str(home / "data"),
        "PATH": str(empty),
    }
    assert_nothing_on_path(env)
    return env


def assert_nothing_on_path(env: dict[str, str]) -> None:
    """``PATH`` is exactly one absolute entry and that directory is empty, so neither ``gh`` nor
    ``glab`` (nor anything else) can be found. Checked right before every live call."""
    entries = env["PATH"].split(os.pathsep)
    assert len(entries) == 1 and os.path.isabs(entries[0]), env["PATH"]
    assert os.listdir(entries[0]) == [], "the scratch PATH directory is not empty"
    assert shutil.which("gh", path=entries[0]) is None
    assert shutil.which("glab", path=entries[0]) is None


class CheckedGateway(ProcessGateway):
    """The real gateway, with the scratch-environment check run before every call."""

    def __init__(self, *, forgectl_bin: str, environ: dict[str, str], deadline: float) -> None:
        super().__init__(forgectl_bin=forgectl_bin, environ=environ, deadline=deadline)
        self.scratch = dict(environ)

    def call(self, *args: Any, **kwargs: Any) -> ForgectlResult:
        assert_nothing_on_path(self.scratch)
        return super().call(*args, **kwargs)


@pytest.fixture
def live_root(tmp_path: Path) -> Iterator[Path]:
    """The scratch state of one live test (HOME, XDG, empty bin); removed afterwards, since the
    large cases leave state behind."""
    root = tmp_path / "live"
    root.mkdir()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def live_gateway(live_root: Path) -> ProcessGateway:
    binary = real_forgectl(live_root)
    return CheckedGateway(forgectl_bin=binary, environ=scratch_environment(live_root), deadline=120)


@pytest.mark.live
def test_the_real_forgectl_is_the_pinned_release_and_the_environment_is_private(
    live_root: Path,
) -> None:
    real_forgectl(live_root)
    env = scratch_environment(live_root)
    assert not any(k in env for k in ("GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "GLAB_TOKEN"))
    assert all(str(live_root) in v for v in env.values())


@pytest.mark.live
@pytest.mark.parametrize("case", EXPRESSIBLE, ids=ids(EXPRESSIBLE))
def test_the_real_forgectl_ends_every_stdin_case_through_the_gateway_as_recorded(
    case: dict[str, Any], live_gateway: ProcessGateway, spy: PopenSpy
) -> None:
    subcommand, args, data = split(case)
    result = live_gateway.call(subcommand, args, stdin=data)
    check_result(case, result, data)
    assert_text_only_on_stdin(spy, case, data)


@pytest.mark.live
@pytest.mark.parametrize("case", CLOSED, ids=ids(CLOSED))
def test_the_real_forgectl_answers_the_closed_forms_as_recorded(
    case: dict[str, Any], live_gateway: ProcessGateway, spy: PopenSpy, live_root: Path
) -> None:
    """The gateway never sends these; forgectl's answer to them is what the fake follows."""
    subcommand, args, data = split(case)
    with pytest.raises(GatewayUsageError):
        live_gateway.call(subcommand, args, stdin=data)
    assert spy.starts == []
    env = scratch_environment(live_root)
    assert_nothing_on_path(env)
    proc = subprocess.run(
        [real_forgectl(live_root), *case["argv"]],
        input=stdin_bytes(case["stdin"]),
        capture_output=True,
        env=env,
        cwd="/",
        timeout=120,
        check=False,
    )
    if "reaches_host" in case:
        assert proc.returncode == 12 and error_code(proc.stdout) == "tool_missing"
    else:
        assert observed_of(proc.returncode, proc.stdout, proc.stderr) == expected_of(case)
