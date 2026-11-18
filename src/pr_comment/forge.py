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
from .gateway import Gateway
from .grammars import GrammarError, check_branch, check_pr, check_reply_target, check_thread
from .lower import LowerFailure, mismatch_outcome, partial_document, require_ok
from .output import error_outcome

MAX_ITEMS: Final = 100_000
MAX_TEXT_BYTES: Final = 4 * 1024 * 1024
MAX_PATH_BYTES: Final = 64 * 1024
MAX_ID_CHARS: Final = 256
MAX_COUNT: Final = 2**31 - 1
MAX_DETAIL_BYTES: Final = 1024

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
_ONE_LINE_BAD_RE: Final = re.compile(r"[\r\n]")


def _fullmatch(pattern: re.Pattern[str], what: str, limit: int = MAX_ID_CHARS) -> AfterValidator:
    def check(value: str) -> str:
        if len(value) > limit or not pattern.fullmatch(value):
            raise ValueError(f"invalid {what}")
        return value

    return AfterValidator(check)


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
Branch = Annotated[str, _grammar(check_branch)]
ThreadId = Annotated[str, _grammar(check_thread)]
CommentId = Annotated[str, _grammar(check_reply_target)]
Body = Annotated[str, _text(MAX_TEXT_BYTES)]
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
    ref: Branch
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


# --- diff, compare, files ------------------------------------------------------------------------


class DiffRead(_Model):
    pr: Pr
    head_sha: Sha
    base_sha: Sha | None
    merge_base_sha: Sha | None
    diff: Body


class CompareRead(_Model):
    pr: Pr
    from_sha: Sha
    to_sha: Sha
    status: Literal["ahead", "behind", "diverged", "identical", "unavailable"]
    diff: Body | None

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
        paths = [f.path for f in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("a path is listed twice")
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
        if pr is not None and _PR_RE.fullmatch(pr):
            echoed: str = value.pr  # type: ignore[attr-defined]
            if echoed.lower() != pr.lower():
                raise self._mismatch(command, "the document is for another PR")
        return value

    def show(self, pr: str) -> Show:
        """``forgectl show <pr>``: the canonical ``pr``, ``pr_id``, ``repo``, ``backend``,
        ``state``, head sha and base. Raises ``GrammarError`` for a ``pr`` that is not a PR
        reference (callers validate first and exit 2 ``invalid_pr``)."""
        return self._read("show", [check_pr(pr)], Show)

    def diff(self, pr: str, *, expect_head: str | None = None) -> DiffRead:
        """``forgectl diff <pr> --json``. With ``expect_head``, a head that differs is exit 20
        ``stale_head`` (``details.head_sha``)."""
        value = self._read("diff", [check_pr(pr)], DiffRead, pr=pr)
        if expect_head is not None and value.head_sha != expect_head:
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
        if type(host) is not str or len(host) > 300 or not _HOST_RE.fullmatch(host):
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
    decides: the same ``pr_id`` on the same backend (a renamed repo) is accepted, anything else is
    exit 2 ``draft_pr_mismatch`` (``details.draft_pr``, ``details.pr``), also a draft ``pr`` that
    is not a PR reference or that forgectl cannot find (exit 10 or ``invalid_pr``). Any other
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
        if code in ("not_found", "invalid_pr"):
            raise _mismatch_of_draft(draft_pr, shown) from None
        raise
    if other.pr_id != shown.pr_id or other.backend != shown.backend:
        raise _mismatch_of_draft(draft_pr, shown)
