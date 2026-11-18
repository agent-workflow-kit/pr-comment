"""The forgectl reads and the field checks of what they print (docs/interface.md: forgectl edge).

``Forge`` runs ``show``, ``diff``, ``compare``, ``files``, ``comments`` and ``whoami`` through a
``Gateway`` and returns models of only the fields pr-comment uses. Everything forgectl prints is
untrusted until checked: each used field is checked against its grammar (``grammars.py``) and the
pinned schema (``tests/forgectl_schemas``), strictly typed (a number is never a string, a bool is
never an integer), with caps on strings and arrays. Unknown fields are ignored so additive
forgectl releases keep working. A missing or ill-typed field, a value that breaks its grammar, a
document that is not the one asked for, or a failure forgectl cannot give for that command is
exit 12 ``lower_contract_mismatch`` (``details.command``, ``details.lower_version``; ``landed``
false and ``retry`` same_command, since a read changes nothing). forgectl's own failures (exit 10,
12, 20 and the documented exit 2 codes) pass through with its envelope in ``lower_error``.

Every method raises ``LowerFailure`` (``lower.py``) carrying the ``Outcome`` the run ends with. It
never catches ``ForgectlInterrupted``: a signal reaches ``cli.main``. Texts and message strings of
forgectl never go into pr-comment's own messages.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import replace
from typing import Annotated, Any, Final, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from .errors import cut_bytes
from .gateway import MAX_STDOUT_BYTES, Gateway
from .grammars import GrammarError, check_branch, check_pr, check_reply_target
from .lower import LowerFailure, mismatch_outcome, partial_document, require_ok
from .output import error_outcome

MAX_ITEMS: Final = 100_000
MAX_TEXT_BYTES: Final = 4 * 1024 * 1024
MAX_DIFF_BYTES: Final = MAX_STDOUT_BYTES  # forgectl says too_large only above its stdout cap
MAX_PATH_BYTES: Final = 64 * 1024
MAX_ID_CHARS: Final = 256
MAX_COUNT: Final = 2**31 - 1
MAX_DETAIL_BYTES: Final = 1024
MAX_BRANCH_CHARS: Final = 1024

# Failures a read can end in (the pinned fake's tables: ``tests/fake_forgectl.py``
# ``ALLOWED_CODES`` and ``ALLOWED_REASONS``). A write's codes (``vetoed``, ``outcome_partial``,
# ``stale_head`` ...) on a read are forgectl breaking its contract.
_HOST_READ_CODES: Final = frozenset(
    {
        "not_found",
        "auth_required",
        "permission_denied",
        "rate_limited",
        "upstream_error",
        "network_error",
        "host_rejected",
        "wrong_host_response",
        "unsupported_host_version",
        "internal",
        "invalid_pr",
        "config_invalid",
        "refused",
        "tool_missing",
    }
)
_READ_CODES: Final = {
    "show": _HOST_READ_CODES,
    "files": _HOST_READ_CODES,
    "comments": _HOST_READ_CODES,
    "diff": _HOST_READ_CODES,
    "compare": _HOST_READ_CODES,
    "whoami": frozenset({"internal", "config_invalid", "refused"}),
}
_READ_REASONS: Final = {
    "show": frozenset({"unknown_host"}),
    "files": frozenset({"unknown_host"}),
    "comments": frozenset({"unknown_host"}),
    "diff": frozenset({"unknown_host", "too_large"}),
    "compare": frozenset({"unknown_host", "too_large"}),
    "whoami": frozenset({"unknown_host"}),
}

# --- value checks --------------------------------------------------------------------------------

_PR_RE: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|#))[A-Za-z0-9._-]+)+#[1-9][0-9]{0,9}"
)
_REPO_RE: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
    r"(?:/(?!\.\.?(?:/|$))[A-Za-z0-9._-]+)+"
)
_HOST_RE: Final = re.compile(
    r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?"
)
_SHA_RE: Final = re.compile(r"[0-9a-f]{40}")
_OPAQUE_RE: Final = re.compile(r"[A-Za-z0-9:/_=-]{1,128}")
_LISTED_THREAD_RE: Final = re.compile(r"gh:(?:rt|ic):[A-Za-z0-9_=-]{1,100}|gl:d:[0-9a-f]{40}")
_ONE_LINE_BAD_RE: Final = re.compile(r"[\n\v\f\r\x85\u2028\u2029]")


def _fullmatch(pattern: re.Pattern[str], what: str, limit: int = MAX_ID_CHARS) -> AfterValidator:
    def check(value: str) -> str:
        if len(value) > limit or not pattern.fullmatch(value):
            raise ValueError(f"invalid {what}")
        return value

    return AfterValidator(check)


def _check_base_ref(value: str) -> str:
    """A base ref as git allows it, one line: 1 to 1024 characters, no control, format, surrogate
    or separator character (forgectl shows those as ``<U+XXXX>`` markers, which pass)."""
    if not 1 <= len(value) <= MAX_BRANCH_CHARS:
        raise ValueError("invalid base ref")
    if any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in value):
        raise ValueError("base ref holds a control character")
    return value


def _grammar(check: Any) -> AfterValidator:
    def run(value: str) -> str:
        if len(value) > MAX_ID_CHARS * 4:
            raise ValueError("too long")
        try:
            return check(value)  # type: ignore[no-any-return]
        except GrammarError as exc:
            raise ValueError(str(exc)) from None

    return AfterValidator(run)


def _text(limit: int, *, one_line: bool = False) -> AfterValidator:
    """Free text from the host: encodable (no lone surrogate), no NUL, at most ``limit`` bytes."""

    def check(value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise ValueError("text holds a lone surrogate") from None
        if size > limit:
            raise ValueError("text too long")
        if "\x00" in value or (one_line and _ONE_LINE_BAD_RE.search(value)):
            raise ValueError("text holds a control character")
        return value

    return AfterValidator(check)


Pr = Annotated[str, _fullmatch(_PR_RE, "pr", 2048)]
RepoId = Annotated[str, _fullmatch(_REPO_RE, "repo", 2048)]
Host = Annotated[str, _fullmatch(_HOST_RE, "host", 300)]
Sha = Annotated[str, _fullmatch(_SHA_RE, "sha", 40)]
Opaque = Annotated[str, _fullmatch(_OPAQUE_RE, "id", 128)]
BaseRef = Annotated[str, AfterValidator(_check_base_ref)]
# A listed thread: a resolvable one (``gh:rt:``, ``gl:d:``) or a GitHub general comment shown as a
# one-comment thread (``gh:ic:``, never resolvable). ``grammars.check_thread`` stays strict for
# resolve targets.
ThreadId = Annotated[str, _fullmatch(_LISTED_THREAD_RE, "thread id", 256)]
CommentId = Annotated[str, _grammar(check_reply_target)]
Body = Annotated[str, _text(MAX_TEXT_BYTES)]
DiffText = Annotated[str, _text(MAX_DIFF_BYTES)]
Path = Annotated[str, _text(MAX_PATH_BYTES, one_line=True)]
Login = Annotated[str, _text(MAX_ID_CHARS * 4, one_line=True), Field(min_length=1)]
Count = Annotated[int, Field(ge=0, le=MAX_COUNT)]
Backend = Literal["github", "gitlab"]


class _Model(BaseModel):
    """Strict (``True`` is not an integer, ``"1"`` is not a number), immutable, unknown fields
    ignored."""

    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")


# --- show ----------------------------------------------------------------------------------------


class _Head(_Model):
    sha: Sha


class _Base(_Model):
    ref: BaseRef
    sha: Sha | None


class Show(_Model):
    """The canonical identity of a PR: ``pr``, ``pr_id``, ``repo`` and ``backend`` are used
    everywhere after this read (dedupe keys, hooks, audit, ``repos.<id>``)."""

    pr: Pr
    pr_id: Opaque
    repo: RepoId
    number: Annotated[int, Field(ge=1, le=9_999_999_999)]
    backend: Backend
    state: Literal["open", "closed", "merged"]
    head: _Head
    base: _Base

    @model_validator(mode="after")
    def _consistent(self) -> Show:
        if self.pr != f"{self.repo}#{self.number}":
            raise ValueError("pr is not repo#number")
        return self

    @property
    def head_sha(self) -> str:
        return self.head.sha

    @property
    def base_ref(self) -> str:
        return self.base.ref

    @property
    def base_sha(self) -> str | None:
        return self.base.sha

    @property
    def host(self) -> str:
        """The host: the repo id up to its first ``/``."""
        return self.repo.split("/", 1)[0]

    def expected_base(self) -> str:
        """The base ref as ``--expected-base`` takes it. ``show`` accepts any one-line ref (git
        allows ``refs/x``, ``HEAD``, a leading ``-``); forwarding needs forgectl's branch grammar
        and raises ``GrammarError`` for one that fails it."""
        return check_branch(self.base.ref)


# --- diff, compare, files ------------------------------------------------------------------------


class DiffRead(_Model):
    pr: Pr
    head_sha: Sha
    base_sha: Sha | None
    merge_base_sha: Sha | None
    diff: DiffText


class CompareRead(_Model):
    pr: Pr
    from_sha: Sha
    to_sha: Sha
    status: Literal["ahead", "behind", "diverged", "identical", "unavailable"]
    diff: DiffText | None

    @model_validator(mode="after")
    def _consistent(self) -> CompareRead:
        # the pinned schema: null on behind, diverged and unavailable; a string otherwise, empty
        # on identical. A missing ``diff`` is a contract break (pr-comment never passes --no-diff).
        if self.status in ("behind", "diverged", "unavailable"):
            if self.diff is not None:
                raise ValueError("diff must be null for this status")
        elif self.diff is None:
            raise ValueError("diff must be a string for this status")
        elif self.status == "identical" and self.diff != "":
            raise ValueError("diff must be empty when identical")
        return self


class FileRead(_Model):
    path: Annotated[str, _text(MAX_PATH_BYTES, one_line=True), Field(min_length=1)]
    previous_path: Annotated[str, _text(MAX_PATH_BYTES, one_line=True), Field(min_length=1)] | None
    status: Literal["added", "modified", "deleted", "renamed"]

    @model_validator(mode="after")
    def _consistent(self) -> FileRead:
        if (self.status == "renamed") != (self.previous_path is not None):
            raise ValueError("previous_path is set exactly when renamed")
        return self


class FilesRead(_Model):
    pr: Pr
    head_sha: Sha
    files: Annotated[list[FileRead], Field(max_length=MAX_ITEMS)]
    files_complete: bool
    changed_files: Count | None

    @model_validator(mode="after")
    def _unique(self) -> FilesRead:
        # a path repeats only for a file that became a symlink or back (GitLab lists the
        # deletion, then the addition)
        statuses: dict[str, list[str]] = {}
        for f in self.files:
            statuses.setdefault(f.path, []).append(f.status)
        for found in statuses.values():
            if len(found) > 1 and found != ["deleted", "added"]:
                raise ValueError("a path is listed twice")
        return self

    @model_validator(mode="after")
    def _count(self) -> FilesRead:
        # forgectl sets files_complete only when the list is as long as changed_files
        counted = self.changed_files is not None and self.changed_files != len(self.files)
        if self.files_complete and counted:
            raise ValueError("changed_files is not the number of files listed")
        return self


# --- comments ------------------------------------------------------------------------------------


class AuthorRead(_Model):
    login: Login | None
    id: Opaque | None
    is_bot: bool | None


class CommentRead(_Model):
    id: CommentId
    author: AuthorRead
    body: Body
    review_id: CommentId | None


class ThreadRead(_Model):
    id: ThreadId
    resolvable: bool
    resolved: bool
    outdated: bool | None
    path: Path | None
    line: Annotated[int, Field(ge=1, le=MAX_COUNT)] | None
    side: Literal["left", "right"] | None
    start_line: Annotated[int, Field(ge=1, le=MAX_COUNT)] | None
    commit_sha: Sha | None
    comments: Annotated[list[CommentRead], Field(min_length=1, max_length=MAX_ITEMS)]

    @model_validator(mode="after")
    def _consistent(self) -> ThreadRead:
        if not self.resolvable and self.resolved:
            raise ValueError("a thread that cannot be resolved is not resolved")
        return self


class CommentsRead(_Model):
    pr: Pr
    head_sha: Sha
    threads: Annotated[list[ThreadRead], Field(max_length=MAX_ITEMS)]
    complete: bool

    @model_validator(mode="after")
    def _unique(self) -> CommentsRead:
        threads = [t.id for t in self.threads]
        comments = [c.id for t in self.threads for c in t.comments]
        if len(set(threads)) != len(threads) or len(set(comments)) != len(comments):
            raise ValueError("an id is listed twice")
        return self


# --- whoami --------------------------------------------------------------------------------------


class _WhoamiHost(_Model):
    host: Host
    backend: Backend
    login: Login | None
    id: Opaque | None


class _HostError(_Model):
    host: Host
    code: Annotated[str, Field(min_length=1, max_length=64)]
    message: Body


class WhoamiRead(_Model):
    """One host's identity. ``id`` is null when forgectl could not tell (then ``partial`` is
    usually true): callers treat that as "unknown", never as an error of their own."""

    host: Host
    backend: Backend
    login: Login | None
    id: Opaque | None
    errors: list[_HostError]

    @property
    def partial(self) -> bool:
        """forgectl exited 12 ``partial_results``: the host failed and ``errors`` says why."""
        return bool(self.errors)


class _WhoamiDocument(_Model):
    hosts: Annotated[list[_WhoamiHost], Field(max_length=MAX_ITEMS)]
    errors: Annotated[list[_HostError], Field(max_length=MAX_ITEMS)]


_M = TypeVar("_M", bound=_Model)

# --- a write's result ----------------------------------------------------------------------------

_WARNING_RE: Final = re.compile(r"[a-z][a-z0-9_]*")
# forgectl's schema (cli/review.json) bounds neither the number of warnings nor their length, so
# a landed answer is never refused for either (the output cap of the gateway is the only limit).
Warning_ = Annotated[str, _fullmatch(_WARNING_RE, "warning", 32 * 1024 * 1024)]
PostId = Annotated[str, _grammar(check_reply_target)]


class PostRead(_Model):
    """The fields of a real ``review`` result pr-comment uses (forgectl ``cli/review.json``)."""

    pr: Pr
    action: str
    ids: Annotated[list[PostId], Field(max_length=MAX_ITEMS)]
    thread_ids: Annotated[list[PostId], Field(max_length=MAX_ITEMS)]
    review_id: PostId | None
    already_posted: bool
    follow_up_done: bool
    neutralized_lines: Count
    markers_removed: Count
    warnings: list[Warning_]
    # ``review`` and ``approve`` carry ``head_verified``, ``reply`` carries ``threaded``: required
    # for those commands by ``read_post``, ignored for the others
    head_verified: bool | None = None
    threaded: bool | None = None
    dry_run: bool


def read_post(document: Any, *, command: str, pr: str, gateway: Gateway) -> PostRead:
    """The checked result of a write that exited 0. A document that is not a real run's answer for
    ``command`` on ``pr`` is ``lower_contract_mismatch`` with the write's own pair (``landed:
    true``, ``retry: none``: forgectl said it succeeded), never a retry."""

    def mismatch(why: str) -> LowerFailure:
        return LowerFailure(mismatch_outcome(command, why, gateway=gateway, write=(True, "none")))

    try:
        read = PostRead.model_validate(document)
    except ValidationError as exc:
        raise mismatch(_why(exc)) from None
    if read.action != command or read.dry_run or _canonical(read.pr) != _canonical(pr):
        raise mismatch("the document is not the real run's result for this PR")
    # a field the command's own schema requires, missing or null, is a bad answer
    if command in ("review", "approve") and "head_verified" not in document:
        raise mismatch("invalid or missing: /head_verified")
    if command == "reply" and read.threaded is None:
        raise mismatch("invalid or missing: /threaded")
    return read


def _why(exc: ValidationError) -> str:
    """The field pointers that failed (never a value: it is untrusted text)."""
    seen: list[str] = []
    for error in exc.errors():
        path = "/" + "/".join(str(part) for part in error["loc"])
        if path not in seen:
            seen.append(path)
        if len(seen) == 5:
            break
    return "invalid or missing: " + ", ".join(seen)


def _stale_head(head_sha: str) -> LowerFailure:
    return LowerFailure(
        error_outcome(
            "refused",
            "the head moved; rerun with the current head",
            reason="stale_head",
            details={"head_sha": head_sha},
        )
    )


def _name_what_was_not_found(failure: LowerFailure, pr: str) -> LowerFailure:
    """forgectl's ``not_found`` on a read is the PR: pr-comment's envelope says so
    (``details.what`` and ``details.id``, docs/interface.md: Error envelope), next to forgectl's
    own details."""
    document = failure.outcome.document
    error: object = document.get("error") if document is not None else None
    if not isinstance(error, dict) or error.get("code") != "not_found":  # pyright: ignore[reportUnknownMemberType]
        return failure
    details: object = error.get("details")  # pyright: ignore[reportUnknownMemberType]
    merged: dict[str, Any] = dict(details) if isinstance(details, dict) else {}  # pyright: ignore[reportUnknownArgumentType]
    merged.update(what="pr", id=cut_bytes(pr, MAX_DETAIL_BYTES))
    named: dict[str, Any] = {**error, "details": merged}  # pyright: ignore[reportUnknownVariableType]
    return LowerFailure(replace(failure.outcome, document={"error": named}))


class Forge:
    """The forgectl reads. ``pr`` arguments are the user's reference (``show``) or the canonical
    ``pr`` of a ``show``; a canonical one must come back in the document, else it is a mismatch."""

    def __init__(self, gateway: Gateway) -> None:
        self._gateway = gateway

    def lower_version(self) -> str | None:
        return self._gateway.lower_version()

    def _mismatch(self, command: str, why: str) -> LowerFailure:
        return LowerFailure(mismatch_outcome(command, why, gateway=self._gateway))

    def _read(
        self, command: str, args: Sequence[str], model: type[_M], *, pr: str | None = None
    ) -> _M:
        result = self._gateway.call(command, args)
        try:
            document = require_ok(
                result,
                gateway=self._gateway,
                codes=_READ_CODES[command],
                reasons=_READ_REASONS[command],
            )
        except LowerFailure as failure:
            raise _name_what_was_not_found(failure, args[0]) from None
        try:
            value = model.model_validate(document)
        except ValidationError as exc:
            raise self._mismatch(command, _why(exc)) from None
        if pr is not None:
            asked = _canonical(pr)
            # a URL argument is parsed by forgectl, so it is not compared
            if asked is not None and asked != _canonical(value.pr):  # type: ignore[attr-defined]
                raise self._mismatch(command, "the document is for another PR")
        return value

    def show(self, pr: str) -> Show:
        """``forgectl show <pr>``: the canonical ``pr``, ``pr_id``, ``repo``, ``backend``,
        ``state``, head sha and base. Raises ``GrammarError`` for a ``pr`` that is not a PR
        reference (callers validate first and exit 2 ``invalid_pr``)."""
        shown = self._read("show", [check_pr(pr)], Show)
        asked = _canonical(pr)
        # a rename changes the path only: the host and the number are the ones asked for
        if asked is not None and asked != (shown.host, shown.number):
            raise self._mismatch("show", "the document is for another PR")
        return shown

    def diff(self, pr: str, *, expect_head: str | None = None) -> DiffRead:
        """``forgectl diff <pr> --json``. With ``expect_head``, a head that differs is exit 20
        ``stale_head`` (``details.head_sha``)."""
        head = _sha_arg(expect_head) if expect_head is not None else None
        value = self._read("diff", [check_pr(pr)], DiffRead, pr=pr)
        if head is not None and value.head_sha != head:
            raise _stale_head(value.head_sha)
        return value

    def compare(self, pr: str, from_sha: str, to_sha: str) -> CompareRead:
        """``forgectl compare <pr> --from=A --to=B`` with the diff. ``from_sha`` of the document
        must be A (else a mismatch); a ``to_sha`` other than B is exit 20 ``stale_head``."""
        a, b = _sha_arg(from_sha), _sha_arg(to_sha)
        value = self._read(
            "compare", [check_pr(pr), f"--from={a}", f"--to={b}"], CompareRead, pr=pr
        )
        if value.from_sha != a:
            raise self._mismatch("compare", "the document compares another commit")
        if value.to_sha != b:
            raise _stale_head(value.to_sha)
        return value

    def files(self, pr: str) -> FilesRead:
        return self._read("files", [check_pr(pr)], FilesRead, pr=pr)

    def comments(self, pr: str) -> CommentsRead:
        return self._read("comments", [check_pr(pr)], CommentsRead, pr=pr)

    def whoami(self, host: str) -> WhoamiRead:
        """``forgectl whoami --host=<host>``: the account for one host. An exit 12
        ``partial_results`` is not a failure here: it returns with ``partial`` true and the host's
        ``errors``, and the caller decides what is fatal (``resolve`` goes on with the author
        unknown)."""
        if (
            type(host) is not str
            or len(host) > 300
            or not _HOST_RE.fullmatch(host)
            or _forgectl_host(host) != host
        ):
            raise GrammarError("invalid host")
        result = self._gateway.call("whoami", [f"--host={host}"])
        partial = partial_document(result) is not None
        if partial:
            document: Any = partial_document(result)
        else:
            document = require_ok(
                result,
                gateway=self._gateway,
                codes=_READ_CODES["whoami"],
                reasons=_READ_REASONS["whoami"],
            )
        try:
            parsed = _WhoamiDocument.model_validate(document)
        except ValidationError as exc:
            raise self._mismatch("whoami", _why(exc)) from None
        problem = _whoami_problem(parsed, host, partial)
        if problem is not None:
            raise self._mismatch("whoami", problem)
        entry = parsed.hosts[0]
        return WhoamiRead(
            host=entry.host,
            backend=entry.backend,
            login=entry.login,
            id=entry.id,
            errors=parsed.errors,
        )


_BUILT_IN_HOSTS: Final = frozenset({"github.com", "gitlab.com"})


def _forgectl_host(host: str) -> str | None:
    """The host as forgectl normalizes it (lowercase, ``:443`` dropped, ``www.`` stripped on a
    built-in host), or None for a port forgectl refuses (leading zero, above 65535)."""
    name, _, port = host.lower().partition(":")
    if port:
        if port.startswith("0") or not 1 <= int(port) <= 65535:
            return None
        if port == "443":
            port = ""
    if not port and name.startswith("www.") and name[4:] in _BUILT_IN_HOSTS:
        name = name[4:]
    return f"{name}:{port}" if port else name


def _canonical(pr: str) -> tuple[str, int] | None:
    """``(host, number)`` of a ``host/path#N`` reference as forgectl reads it, or None for a URL
    or anything else (forgectl parses those). The host is lowercased first, as forgectl does."""
    host, slash, rest = pr.partition("/")
    if not slash or not _PR_RE.fullmatch(f"{host.lower()}/{rest}"):
        return None
    normalized = _forgectl_host(host)
    if normalized is None:
        return None
    return normalized, int(pr.rsplit("#", 1)[1])


def _sha_arg(value: str) -> str:
    text = value.lower() if isinstance(value, str) else value
    if not isinstance(text, str) or not _SHA_RE.fullmatch(text):
        raise GrammarError("invalid sha (40 hex digits)")
    return text


def _whoami_problem(document: _WhoamiDocument, host: str, partial: bool) -> str | None:
    """What is wrong with a ``whoami --host`` answer beyond its fields: it is for exactly that
    host, and it lists errors exactly when forgectl exited 12."""
    if len(document.hosts) != 1 or document.hosts[0].host != host:
        return "the document is for another host"
    if any(error.host != host for error in document.errors):
        return "an error names another host"
    if partial != bool(document.errors):
        return "errors do not fit the exit status"
    return None


_ASCII_LOWER: Final = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}


def _mismatch_of_draft(draft_pr: str, shown: Show) -> LowerFailure:
    return LowerFailure(
        error_outcome(
            "draft_pr_mismatch",
            "the draft is for another pull request",
            details={"draft_pr": cut_bytes(draft_pr, MAX_DETAIL_BYTES), "pr": shown.pr},
        )
    )


def match_draft_pr(forge: Forge, draft_pr: str, shown: Show) -> None:
    """Check a draft's ``pr`` against ``show``'s (docs/interface.md: Draft fields).

    Host and path are compared in ASCII lowercase. When they differ, ``forgectl show <draft pr>``
    decides: the same ``pr_id`` on the same backend, host and number (a renamed repo changes the
    path only) is accepted, anything else is exit 2 ``draft_pr_mismatch`` (``details.draft_pr``,
    ``details.pr``), also a draft ``pr`` that is not a PR reference or that forgectl cannot find
    (exit 10, ``invalid_pr``, or ``unknown_host`` for a host the config does not declare). Any other
    failure of that read (auth, rate limit, a contract mismatch) is passed on as it is. Raises
    ``LowerFailure`` and never calls forgectl with a value that fails ``check_pr``.
    """
    if draft_pr.translate(_ASCII_LOWER) == shown.pr.translate(_ASCII_LOWER):
        return
    try:
        check_pr(draft_pr)
    except GrammarError:
        raise _mismatch_of_draft(draft_pr, shown) from None
    try:
        other = forge.show(draft_pr)
    except LowerFailure as failure:
        document = failure.outcome.document
        error: object = document.get("error") if document is not None else None
        code = error.get("code") if isinstance(error, dict) else None  # pyright: ignore[reportUnknownMemberType]
        reason = error.get("reason") if isinstance(error, dict) else None  # pyright: ignore[reportUnknownMemberType]
        if code in ("not_found", "invalid_pr") or (code == "refused" and reason == "unknown_host"):
            raise _mismatch_of_draft(draft_pr, shown) from None
        raise
    same_host = other.host == shown.host and other.number == shown.number
    if other.pr_id != shown.pr_id or other.backend != shown.backend or not same_host:
        raise _mismatch_of_draft(draft_pr, shown)
