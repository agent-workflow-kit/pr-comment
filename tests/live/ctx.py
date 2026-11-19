"""What a scenario sees: one target, the runner, and helpers that behave in every mode.

In ``plan`` and ``preflight`` mode nothing real was written, so values that only a write produces
(ids, threads) are placeholders such as ``<comment_id>`` and every ``check`` is a no-op. In ``run``
mode a check that fails raises ``LiveFailure`` naming the row it belongs to.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from . import helpers
from .helpers import Html
from .render import Renderer
from .runner import LiveFailure, Outcome, Runner
from .settings import PLACEHOLDER_REF, LiveSettings, Target

ZERO_SHA = "0" * 40


class Ctx:
    def __init__(self, runner: Runner, target: Target | None, scenario: str) -> None:
        self.runner = runner
        self.settings: LiveSettings = runner.settings
        self.target = target
        self.scenario = scenario
        runner.scenario = scenario
        self.renderer = Renderer(runner)
        self._whoami: dict[str, Any] | None = None
        self._show: dict[str, Any] | None = None
        self._tap_seen = 0
        self.values: dict[str, Any] = {}

    # --- mode -----------------------------------------------------------------------------------

    @property
    def live(self) -> bool:
        return self.settings.writes

    @property
    def backend(self) -> str:
        return self.target.backend if self.target else "github"

    @property
    def ref(self) -> str:
        return self.target.ref if self.target else PLACEHOLDER_REF["github"]

    @property
    def tag(self) -> str:
        return self.settings.tag

    @property
    def github(self) -> bool:
        return self.backend == "github"

    # --- checks ---------------------------------------------------------------------------------

    def check(self, condition: object, message: str, row: str = "") -> None:
        """Fail (in run mode only) with ``message``; never put a secret or a body in it."""
        __tracebackhide__ = True
        if self.live and not condition:
            where = f" [{row}]" if row else ""
            raise LiveFailure(f"{self.scenario}: {message}{where}")

    def observe(self, row: str, key: str, value: Any) -> None:
        """Record a result for the report (design.md's Live verification table)."""
        self.runner.observe(row, key, value)

    def want(self, out: Outcome, code: int = 0, error: str | None = None, row: str = "") -> None:
        """The step ended with exit ``code`` (and error code ``error``). In run mode a step that
        did not run fails here: it cannot be taken for a pass."""
        __tracebackhide__ = True
        if not self.live:
            return
        self.check(out.real, f"{out.step.label}: the step did not run", row)
        shown = f"exit {out.code}, code {out.error_code}, stderr {out.stderr[-300:]!r}"
        self.check(out.code == code, f"{out.step.label}: wanted exit {code}, got {shown}", row)
        if error is not None:
            self.check(
                out.error_code == error or out.error.get("reason") == error,
                f"{out.step.label}: wanted error {error}, got {shown}",
                row,
            )

    def placeholder(self, name: str) -> str:
        return f"<{name}>"

    def value(self, out: Outcome, key: str, name: str | None = None) -> Any:
        """``out.doc[key]`` once a real write ran, else a placeholder for the plan."""
        if self.live and out.real and out.doc is not None:
            return out.doc.get(key)
        return self.placeholder(name or key)

    def ids(self, out: Outcome) -> list[str]:
        if self.live and out.real and out.doc is not None:
            return [i for i in out.doc.get("ids") or [] if isinstance(i, str)]
        return [self.placeholder("comment_id")]

    # --- text -----------------------------------------------------------------------------------

    def text(self, label: str, body: str) -> str:
        """A body for this scenario: the text, the run's tag and the label, so it can be found."""
        return f"{body}\n\n{self.tag} {self.scenario}/{label}"

    def label_tag(self, label: str) -> str:
        return f"{self.tag} {self.scenario}/{label}"

    # --- reads ----------------------------------------------------------------------------------

    def show(self) -> dict[str, Any]:
        if self._show is None:
            out = self.runner.fg("show", "show", "--json", self.ref)
            doc = out.doc if out.real and out.code == 0 and out.doc else None
            self._show = doc or {
                "pr": "host/path#1",
                "head": {"sha": ZERO_SHA, "ref": "topic"},
                "base": {"ref": "main"},
                "state": "open",
                "author": {"login": "<author>", "id": "<id>"},
                "repo": "host/path",
            }
        return self._show

    def pins(self, head: str | None = None) -> list[str]:
        """``--expected-head`` and ``--expected-base`` for a raw forgectl ``review`` or ``approve``:
        forgectl exits 2 ``usage`` without both unless the run is interactive, which a suite call is
        not. The head is the target's current one unless ``head`` says otherwise."""
        shown = self.show()
        pinned = head or (shown.get("head") or {}).get("sha") or ZERO_SHA
        return [
            f"--expected-head={pinned}",
            f"--expected-base={(shown.get('base') or {}).get('ref')}",
        ]

    def whoami(self) -> dict[str, Any]:
        """This account on the target's host (``login``, ``id``)."""
        if self._whoami is None:
            out = self.runner.fg("whoami", "whoami", "--json")
            host = self.target.host if self.target else None
            found: dict[str, Any] = {}
            if out.real and out.doc:
                for entry in out.doc.get("hosts") or []:
                    if entry.get("host") == host:
                        found = entry
            self._whoami = found or {"login": "<me>", "id": "<me_id>"}
        return self._whoami

    def comments(self) -> list[dict[str, Any]]:
        out = self.runner.fg("comments", "comments", "--json", self.ref)
        return helpers.flatten_comments(out.doc) if out.real and out.code in (0, 12) else []

    def threads(self) -> list[dict[str, Any]]:
        out = self.runner.fg("threads", "comments", "--json", self.ref)
        return list((out.doc or {}).get("threads") or []) if out.real else []

    def posted(self, label: str) -> list[Mapping[str, Any]]:
        """Comments on the target carrying this scenario's label tag."""
        return helpers.tagged(self.comments(), self.label_tag(label))

    def stored_body(self, label: str) -> str | None:
        found = self.posted(label)
        return str(found[0].get("body", "")) if found else None

    def diff_lines(self, count: int) -> list[tuple[str, int]]:
        """``count`` added lines of the target's diff (cycled), or placeholders."""
        out = self.runner.fg("diff", "diff", "--json", self.ref)
        diff = str((out.doc or {}).get("diff") or "") if out.real and out.code == 0 else ""
        picked = helpers.pick_lines(helpers.added_lines(diff), count)
        if not picked:
            self.check(False, "the target's diff has no added line; it needs a PR that adds lines")
            return [("<path>", i + 1) for i in range(count)]
        return picked

    @property
    def may_approve(self) -> bool:
        """Every approve write is behind PR_COMMENT_LIVE_APPROVE=1 in run mode."""
        return not self.live or self.settings.approve

    def retarget(self, ref: str) -> Ctx:
        """The same scenario on another PR of the same kind of host (the closed PR)."""
        target = Target(self.target.key, self.target.backend, ref) if self.target else None
        return Ctx(self.runner, target, self.scenario)

    def render(self, forge_id: str) -> Html | None:
        if not self.live or forge_id.startswith("<"):
            return None
        host = self.target.host if self.target else None
        html = self.renderer.html(self.backend, host, forge_id)
        self.observe("render", "available", html is not None)
        return html

    def tap_since(self) -> list[list[str]]:
        calls = self.runner.tap_calls()
        fresh = calls[self._tap_seen :]
        self._tap_seen = len(calls)
        return fresh

    # --- drafts and posts -----------------------------------------------------------------------

    def draft(
        self,
        label: str,
        findings: Sequence[Mapping[str, Any]],
        *,
        body: str = "",
        event: str = "comment",
        head: str | None = None,
        tag_body: bool = True,
    ) -> dict[str, Any]:
        """A review draft anchored at the target's current head (``draft new``), the findings and
        a body carrying the label tag. Its id is fixed by the run id and the label."""
        out = self.runner.pc("draft-new", "draft", "new", self.ref, actor=None)
        base: dict[str, Any] = (
            out.doc
            if out.real and out.code == 0 and out.doc
            else {
                "schema_version": 1,
                "pr": "host/path#1",
                "anchor": {
                    "head_sha": ZERO_SHA,
                    "base_ref": "main",
                    "base_sha": None,
                    "merge_base_sha": None,
                },
            }
        )
        base = json.loads(json.dumps(base))
        base["draft_id"] = helpers.draft_id(self.settings.run_id, f"{self.scenario}/{label}")
        base["event"] = event
        base["body"] = f"{body}\n\n{self.label_tag(label)}".strip() if tag_body else body
        base["findings"] = [dict(f) for f in findings]
        if head:
            base["anchor"]["head_sha"] = head
        return base

    def post_review(self, label: str, draft: Mapping[str, Any], *flags: str) -> Outcome:
        return self.runner.pc(
            f"review-{label}",
            "review",
            self.ref,
            "--draft-file",
            "-",
            *flags,
            stdin=json.dumps(draft).encode("utf-8"),
        )

    def comment(self, label: str, body: str, *flags: str, actor: str | None = None) -> Outcome:
        """``comment`` with ``body`` as given (the label tag is added by ``text``)."""
        kwargs: dict[str, Any] = {"stdin": body.encode("utf-8")}
        if actor:
            kwargs["actor"] = actor
        return self.runner.pc(
            f"comment-{label}", "comment", self.ref, "--body-file", "-", *flags, **kwargs
        )

    def reply(self, label: str, to: str, body: str, *flags: str) -> Outcome:
        return self.runner.pc(
            f"reply-{label}",
            "reply",
            self.ref,
            "--to",
            to,
            "--body-file",
            "-",
            *flags,
            stdin=body.encode("utf-8"),
        )

    def raw_comment(self, label: str, body: str) -> Outcome:
        """A comment posted by forgectl directly, for a host question pr-comment's text pipeline
        would change the answer to (a literal mention as a positive control, a forbidden fence)."""
        return self.runner.fg(
            f"raw-comment-{label}",
            "comment",
            "--json",
            self.ref,
            "--body-file",
            "-",
            "--actor",
            "auto:live-suite",
            stdin=body.encode("utf-8"),
            write=True,
        )

    def raw_review(self, label: str, document: Mapping[str, Any]) -> Outcome:
        return self.runner.fg(
            f"raw-review-{label}",
            "review",
            "--json",
            self.ref,
            "--file",
            "-",
            "--actor",
            "auto:live-suite",
            *self.pins(),
            stdin=json.dumps(document).encode("utf-8"),
            write=True,
        )
