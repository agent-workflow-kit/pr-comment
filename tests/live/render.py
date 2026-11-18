"""How a host renders a posted comment, through the host's own CLI (``gh`` or ``glab``).

pr-comment and the suite's ``Runner`` never call ``gh`` or ``glab`` for the tool itself; this module
is a test helper, used only with ``PR_COMMENT_LIVE_RENDER=1`` and only to read. Each lookup is one
GraphQL query by the id forgectl printed (the part after the prefix). Whether those ids are the
host's node ids is itself unverified: when a lookup returns nothing the caller records
"unavailable" and the row falls back to its manual step in docs/live-checklist.md.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from .helpers import Html
from .runner import Runner
from .settings import redact

GITHUB_QUERY = (
    "query($id: ID!) { node(id: $id) { __typename "
    "... on IssueComment { bodyHTML } ... on PullRequestReviewComment { bodyHTML } "
    "... on PullRequestReview { bodyHTML } } }"
)
GITLAB_QUERY = "query($id: NoteID!) { note(id: $id) { bodyHtml } }"


def _split(forge_id: str) -> tuple[str, str]:
    """``gh:rc:abc`` -> (``rc``, ``abc``); ``gl:n:12`` -> (``n``, ``12``)."""
    parts = forge_id.split(":", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("", forge_id)


class Renderer:
    def __init__(self, runner: Runner, runner_call: Callable[..., Any] | None = None) -> None:
        self.runner = runner
        self._call = runner_call or subprocess.run

    def available(self, backend: str) -> bool:
        return (
            self.runner.settings.render
            and shutil.which(
                "gh" if backend == "github" else "glab", path=self.runner.env.get("PATH", "")
            )
            is not None
        )

    def _graphql(self, tool: str, host: str | None, query: str, variable: str, value: str) -> Any:
        cmd = [tool, "api", "graphql", "-f", f"query={query}", "-f", f"{variable}={value}"]
        if host and host not in ("github.com", "gitlab.com"):
            cmd[2:2] = ["--hostname", host]
        done = self._call(
            cmd,
            capture_output=True,
            env=self.runner.child_env(),
            cwd="/",
            timeout=120,
            check=False,
        )
        if done.returncode != 0:
            return None
        try:
            return json.loads(done.stdout.decode("utf-8", "replace"))
        except ValueError:
            return None

    def html(self, backend: str, host: str | None, forge_id: str) -> Html | None:
        """The rendered body of one comment, review or note, or None when it cannot be read."""
        if not self.available(backend):
            return None
        kind, raw = _split(forge_id)
        if backend == "github":
            doc = self._graphql("gh", host, GITHUB_QUERY, "id", raw)
            node = ((doc or {}).get("data") or {}).get("node") or {}
            source = node.get("bodyHTML")
        else:
            source = None
            for gid in (f"gid://gitlab/Note/{raw}", f"gid://gitlab/DiffNote/{raw}"):
                doc = self._graphql("glab", host, GITLAB_QUERY, "id", gid)
                note = ((doc or {}).get("data") or {}).get("note") or {}
                source = note.get("bodyHtml")
                if source:
                    break
        return Html(source) if isinstance(source, str) and source else None

    def describe_failure(self, text: str) -> str:
        return redact(text, self.runner.env)
