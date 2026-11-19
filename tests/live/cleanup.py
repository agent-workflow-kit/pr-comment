"""The default cleanup of a run: delete what the host lets a client delete, list the rest.

Used at teardown for every id in the run's ledger when ``PR_COMMENT_LIVE_CLEANUP_CMD`` is not set
(and ``PR_COMMENT_LIVE_KEEP`` is not ``1``). Only ids this run wrote are touched, only on a PR whose
repo is on ``PR_COMMENT_LIVE_ALLOW``, and only a general (issue) comment, an inline review comment
or a GitLab note: those are what a host lets the author delete. A submitted review, an approval and
a resolved thread cannot be deleted, so they are listed for a human. Like ``render.py`` this is a
test helper that calls ``gh`` or ``glab`` itself (pr-comment and forgectl have no delete): it needs
credentials in environment variables (``GH_TOKEN``, ``GITLAB_TOKEN``), because the child
environment has a private ``HOME``. A problem is a note with the exit status only, never the
output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .settings import parse_ref, repo_of_ref

if TYPE_CHECKING:
    from .runner import Runner

TIMEOUT = 120.0

# kind (the middle part of a forgectl id) -> the GraphQL mutation that deletes it
GITHUB_DELETES = {
    "ic": "deleteIssueComment",  # a general PR comment
    "rc": "deletePullRequestReviewComment",  # an inline review comment
}
# kinds a host cannot delete (or that this suite does not undo), with the reason shown to a human
KEPT = {
    "r": "a submitted review (hosts cannot delete it)",
    "rt": "a review thread (it stays, resolved or not)",
    "d": "a GitLab discussion (delete its notes by hand)",
}


def mutation(name: str) -> str:
    return f"mutation($id: ID!) {{ {name}(input: {{id: $id}}) {{ clientMutationId }} }}"


def _kind(forge_id: str) -> tuple[str, str, str]:
    """``gh:ic:IC_1`` -> (``gh``, ``ic``, ``IC_1``); an id of another shape -> empty parts."""
    parts = forge_id.split(":", 2)
    return (parts[0], parts[1], parts[2]) if len(parts) == 3 and parts[2] else ("", "", "")


def plan_delete(pr: str, forge_id: str) -> tuple[list[str] | None, str | None]:
    """The CLI call (argv) that deletes ``forge_id`` on ``pr``, or the reason it is not deleted."""
    parsed = parse_ref(pr)
    backend, kind, raw = _kind(forge_id)
    if parsed is None:
        return None, "its PR reference cannot be read"
    host, path, number = parsed
    hostname = [] if host in ("github.com", "gitlab.com") else ["--hostname", host]
    if backend == "gh" and kind in GITHUB_DELETES:
        query = mutation(GITHUB_DELETES[kind])
        return ["gh", "api", *hostname, "graphql", "-f", f"query={query}", "-f", f"id={raw}"], None
    if backend == "gl" and kind == "n" and raw.isdigit():
        route = f"projects/{quote(path, safe='')}/merge_requests/{number}/notes/{raw}"
        return ["glab", "api", *hostname, "-X", "DELETE", route], None
    return None, KEPT.get(kind, "no known way to delete it")


def delete_posted(
    runner: Runner, row: dict[str, str], call: Callable[..., Any] = subprocess.run
) -> str | None:
    """Delete one ledger row's comment; None when it is gone, else the note for a human."""
    pr, forge_id = row["pr"], row["id"]
    where = f"left on {pr}: {forge_id}"
    if not runner.settings.target_allowed(repo_of_ref(pr)):
        return f"{where} (its repo is not on the allowlist, so it was not touched)"
    argv, why = plan_delete(pr, forge_id)
    if argv is None:
        return f"{where} ({why})"
    tool = shutil.which(argv[0], path=runner.env.get("PATH", ""))
    if tool is None:
        return (
            f"{where} ({argv[0]} was not found on PATH: delete it by hand "
            "or set PR_COMMENT_LIVE_CLEANUP_CMD)"
        )
    try:
        done = call(
            [tool, *argv[1:]],
            capture_output=True,
            env=runner.child_env(),
            cwd="/",
            timeout=TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{where} (deleting it failed: {exc.__class__.__name__})"
    if done.returncode != 0 or _has_errors(done.stdout):
        return f"{where} (deleting it failed: {argv[0]} exit {done.returncode})"
    runner.ledger_event(row, "deleted")
    return None


def _has_errors(stdout: bytes | str) -> bool:
    """A GraphQL answer with an ``errors`` member failed even when the CLI exited 0."""
    text = stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError:
        return False
    return isinstance(doc, dict) and bool(doc.get("errors"))
