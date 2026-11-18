"""What the live suite may do, read from the environment.

The environment is captured once, when this module is imported (collection time, before the autouse
fixture of ``tests/conftest.py`` clears every ``PR_COMMENT_*`` variable and the ``PATH`` of each
test). Nothing here prints a variable's value: a problem names the variable and says what is wrong.

Variables (all optional unless a problem says otherwise; the full list is in
``docs/live-checklist.md``):

- ``PR_COMMENT_LIVE=1``: the opt-in. Without it every host-facing test is skipped.
- ``PR_COMMENT_LIVE_MODE``: ``plan`` (default; prints the steps, calls nothing), ``preflight``
  (reads only: ``--dry-run`` on every write) or ``run`` (writes to the targets).
- ``PR_COMMENT_LIVE_ALLOW``: comma-separated repo ids (``host/path``, exact, no wildcard). A target
  whose repo is not listed is refused, before any call and again after ``forgectl show``.
- ``PR_COMMENT_LIVE_<KEY>_PR`` with KEY one of GITHUB, GITLAB, GITHUB_ENTERPRISE,
  GITLAB_SELFHOSTED: the throwaway PR or MR; ``_OLD_HEAD`` (an earlier head of it), ``_CLOSED_PR``
  and ``_APPROVE_PR`` (a PR not authored by the account) are optional.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REAL_ENV: dict[str, str] = dict(os.environ)

OPT_IN = "PR_COMMENT_LIVE"
MODES = ("plan", "preflight", "run")
# KEY -> backend. The two enterprise keys are for hosts other than github.com and gitlab.com.
TARGET_KEYS: dict[str, str] = {
    "GITHUB": "github",
    "GITLAB": "gitlab",
    "GITHUB_ENTERPRISE": "github",
    "GITLAB_SELFHOSTED": "gitlab",
}
PLACEHOLDER_REF = {"github": "github.com/acme/widgets#1", "gitlab": "gitlab.com/acme/widgets#1"}

SECRET_NAME_RE = re.compile(
    r"TOKEN|SECRET|PASSW|CREDENTIAL|KEY|AUTH|WEBHOOK|COOKIE|SESSION|PRIVATE", re.IGNORECASE
)
REPO_ID_RE = re.compile(r"^[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9._-]+(?:/[a-z0-9._-]+)*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_RE = re.compile(r"^[a-z0-9]{6,24}$")
_URL_RE = re.compile(
    r"^https?://([A-Za-z0-9.-]+(?::[0-9]{1,5})?)/(.+?)/(?:-/)?(?:pull|merge_requests)/[0-9]+/?$"
)
_SHORT_RE = re.compile(r"^([A-Za-z0-9.-]+(?::[0-9]{1,5})?)/(.+)#[0-9]+$")


class LiveConfigError(Exception):
    """A setting the suite refuses; the message names variables, never their values."""


def repo_of_ref(ref: str) -> str | None:
    """The lowercase repo id (``host/path``) a PR argument names, or None when it cannot be told
    from the text (the suite then refuses the target: it never guesses which repo it writes to)."""
    for pattern in (_URL_RE, _SHORT_RE):
        match = pattern.match(ref)
        if match:
            return f"{match.group(1)}/{match.group(2)}".lower()
    return None


@dataclass(frozen=True)
class Target:
    """One throwaway PR or MR and what goes with it."""

    key: str
    backend: str
    ref: str
    old_head: str | None = None
    closed_ref: str | None = None
    approve_ref: str | None = None

    @property
    def repo(self) -> str | None:
        return repo_of_ref(self.ref)

    @property
    def host(self) -> str | None:
        repo = self.repo
        return repo.split("/", 1)[0] if repo else None

    @property
    def is_enterprise(self) -> bool:
        return self.key in ("GITHUB_ENTERPRISE", "GITLAB_SELFHOSTED")

    @property
    def name(self) -> str:
        return self.key.lower()


@dataclass(frozen=True)
class LiveSettings:
    opted_in: bool
    mode: str
    allow: tuple[str, ...]
    targets: Mapping[str, Target]
    forgectl_bin: str
    forgectl_config: str | None
    report_dir: Path | None
    render: bool
    approve: bool
    slow: bool
    cleanup_cmd: str | None
    fs_dir: Path | None
    gitlab_group: str | None
    run_id: str
    problems: tuple[str, ...]

    @property
    def tag(self) -> str:
        """Text every body of this run carries, so what it posted can be found and cleaned up."""
        return f"live-{self.run_id}"

    @property
    def writes(self) -> bool:
        return self.mode == "run"

    def skip_reason(self) -> str | None:
        """Why the host-facing tests do not run (None: they may)."""
        if not self.opted_in:
            return f"live host tests need {OPT_IN}=1 (see docs/live-checklist.md)"
        return None

    def refusals(self) -> list[str]:
        """What stops an opted-in run: every item names a variable."""
        found = list(self.problems)
        if not self.targets and self.mode != "plan":
            found.append(
                "no target: set PR_COMMENT_LIVE_GITHUB_PR or PR_COMMENT_LIVE_GITLAB_PR "
                f"(mode {self.mode} needs one; plan mode needs none)"
            )
        return found

    def target_allowed(self, repo: str | None) -> bool:
        """Is ``repo`` (a repo id) on the allowlist? An unknown repo never is."""
        return repo is not None and repo.lower() in self.allow


def redact(text: str, env: Mapping[str, str] | None = None) -> str:
    """``text`` with the value of every secret-looking variable replaced by its name."""
    source = REAL_ENV if env is None else env
    for name, value in sorted(source.items(), key=lambda item: -len(item[1])):
        if len(value) >= 4 and SECRET_NAME_RE.search(name):
            text = text.replace(value, f"<{name}>")
    return text


def _new_run_id() -> str:
    return time.strftime("%m%d%H%M", time.gmtime()) + secrets.token_hex(2)


def from_env(env: Mapping[str, str] | None = None) -> LiveSettings:
    """Parse the environment (``REAL_ENV`` by default). Never raises: ``problems`` lists faults."""
    source = REAL_ENV if env is None else env
    problems: list[str] = []

    def get(name: str) -> str | None:
        value = source.get(name, "").strip()
        return value or None

    opted_in = get(OPT_IN) == "1"
    if get(OPT_IN) not in (None, "0", "1"):
        problems.append(f"{OPT_IN} must be 1 (to opt in) or unset")

    mode = get("PR_COMMENT_LIVE_MODE") or "plan"
    if mode not in MODES:
        problems.append(f"PR_COMMENT_LIVE_MODE must be one of {', '.join(MODES)}")
        mode = "plan"

    allow: list[str] = []
    raw_allow = get("PR_COMMENT_LIVE_ALLOW")
    for item in (raw_allow or "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not REPO_ID_RE.match(item):
            problems.append("PR_COMMENT_LIVE_ALLOW has an entry that is not an exact repo id")
            continue
        allow.append(item)
    if mode != "plan" and not allow:
        problems.append(
            "PR_COMMENT_LIVE_ALLOW is required (repo ids, comma separated) beyond plan mode"
        )

    targets: dict[str, Target] = {}
    for key, backend in TARGET_KEYS.items():
        prefix = f"PR_COMMENT_LIVE_{key}"
        ref = get(f"{prefix}_PR")
        if ref is None:
            continue
        old_head = get(f"{prefix}_OLD_HEAD")
        if old_head is not None and not SHA_RE.match(old_head.lower()):
            problems.append(f"{prefix}_OLD_HEAD must be 40 hex characters")
            old_head = None
        target = Target(
            key,
            backend,
            ref,
            old_head.lower() if old_head else None,
            get(f"{prefix}_CLOSED_PR"),
            get(f"{prefix}_APPROVE_PR"),
        )
        for label, value in (
            ("_PR", ref),
            ("_CLOSED_PR", target.closed_ref),
            ("_APPROVE_PR", target.approve_ref),
        ):
            if value is None:
                continue
            repo = repo_of_ref(value)
            if repo is None:
                problems.append(f"{prefix}{label} is not a PR URL or host/path#N reference")
            elif repo not in allow:
                problems.append(f"{prefix}{label} names a repo that PR_COMMENT_LIVE_ALLOW lacks")
        if target.is_enterprise and target.host in ("github.com", "gitlab.com"):
            problems.append(f"{prefix}_PR must name a host other than github.com and gitlab.com")
        elif not target.is_enterprise and target.host not in (None, "github.com", "gitlab.com"):
            problems.append(f"{prefix}_PR names a host that needs the ENTERPRISE or SELFHOSTED key")
        targets[key] = target

    report = get("PR_COMMENT_LIVE_REPORT_DIR")
    report_dir = Path(report) if report else None
    if report_dir is not None and not report_dir.is_absolute():
        problems.append("PR_COMMENT_LIVE_REPORT_DIR must be an absolute path")
        report_dir = None
    if mode == "run" and report_dir is None:
        problems.append(
            "PR_COMMENT_LIVE_REPORT_DIR is required in run mode (the ledger of what was posted)"
        )

    cleanup = get("PR_COMMENT_LIVE_CLEANUP_CMD")
    if cleanup is not None and not cleanup.startswith("/"):
        problems.append("PR_COMMENT_LIVE_CLEANUP_CMD must be an absolute path to an executable")
        cleanup = None

    fs = get("PR_COMMENT_LIVE_FS_DIR")
    fs_dir = Path(fs) if fs else None
    if fs_dir is not None and not fs_dir.is_absolute():
        problems.append("PR_COMMENT_LIVE_FS_DIR must be an absolute path")
        fs_dir = None

    run_id = get("PR_COMMENT_LIVE_RUN_ID") or _new_run_id()
    if not RUN_ID_RE.match(run_id):
        problems.append("PR_COMMENT_LIVE_RUN_ID must be 6 to 24 characters of a-z and 0-9")
        run_id = _new_run_id()

    return LiveSettings(
        opted_in=opted_in,
        mode=mode,
        allow=tuple(allow),
        targets=targets,
        forgectl_bin=get("PR_COMMENT_LIVE_FORGECTL") or "forgectl",
        forgectl_config=get("PR_COMMENT_LIVE_FORGECTL_CONFIG"),
        report_dir=report_dir,
        render=get("PR_COMMENT_LIVE_RENDER") == "1",
        approve=get("PR_COMMENT_LIVE_APPROVE") == "1",
        slow=get("PR_COMMENT_LIVE_SLOW") == "1",
        cleanup_cmd=cleanup,
        fs_dir=fs_dir,
        gitlab_group=get("PR_COMMENT_LIVE_GITLAB_GROUP"),
        run_id=run_id,
        problems=tuple(problems),
    )
