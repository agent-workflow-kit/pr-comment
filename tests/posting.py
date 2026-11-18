"""Builders shared by the posting-plan tests: a draft, the ``show`` of its PR, a plan context and a
review source made the way ``prepare.py`` makes one (through ``reanchor``)."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from pr_comment.config import LoadedConfig, RepoSettings
from pr_comment.diffmap import DiffMapper
from pr_comment.draft import Draft, validate_document
from pr_comment.forge import Show
from pr_comment.plan import Context, ReviewSource, review_source
from pr_comment.reanchor import reanchor
from tests import forgectl_harness as h

A = h.HEAD
B = h.OTHER_HEAD
BASE_SHA = h.BASE_SHA
PR = h.PR
DRAFT_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
MB = "1" * 40
Doc = dict[str, Any]


def show_of(pr: str = PR, *, backend: str = "github", head: str = A, **overrides: Any) -> Show:
    doc = h.show_doc(pr, backend=backend, **overrides)
    doc["head"] = {**doc["head"], "sha": head}
    return Show.model_validate(doc)


def settings(**config: Any) -> RepoSettings:
    loaded = LoadedConfig.defaults()
    if config:
        loaded = LoadedConfig(
            config=loaded.config.model_copy(update=config),
            config_hash=loaded.config_hash,
            config_path=None,
        )
    return loaded.for_repo("github.com/acme/widgets")


def context(
    show: Show | None = None,
    *,
    actor: str = "human:alice",
    decision_ids: Sequence[str] = (),
    allow_mentions: Sequence[str] = (),
    dedupe_key: str | None = None,
    override_forgectl: bool = False,
    repo_settings: RepoSettings | None = None,
) -> Context:
    return Context(
        show=show or show_of(),
        actor=actor,
        decision_ids=tuple(decision_ids),
        settings=repo_settings or settings(),
        me_name="alice",
        tool_version="0.1.0",
        allow_mentions=tuple(allow_mentions),
        dedupe_key=dedupe_key,
        override_forgectl=override_forgectl,
    )


def finding(
    fid: str,
    path: str = "a.py",
    line: int | None = 3,
    *,
    start: int | None = None,
    side: str | None = None,
    body: str | None = None,
    **extra: Any,
) -> Doc:
    out: Doc = {"id": fid, "path": path, "line": line, "body": body or f"body {fid}"}
    if start is not None:
        out["start_line"] = start
    if side is not None:
        out["side"] = side
    out.update(extra)
    return out


def draft_doc(
    findings: Sequence[Doc],
    *,
    event: str = "comment",
    body: str = "summary",
    unanchored: Sequence[Doc] | None = None,
    head: str = A,
    merge_base: str | None = MB,
    pr: str = PR,
) -> Doc:
    doc: Doc = {
        "schema_version": 1,
        "draft_id": DRAFT_ID,
        "pr": pr,
        "anchor": {
            "head_sha": head,
            "base_ref": "main",
            "base_sha": BASE_SHA,
            "merge_base_sha": merge_base,
        },
        "event": event,
        "body": body,
        "findings": [copy.deepcopy(f) for f in findings],
    }
    if unanchored is not None:
        doc["unanchored"] = [copy.deepcopy(u) for u in unanchored]
    assert validate_document(doc) == [], validate_document(doc)
    return doc


def source_of(
    doc: Mapping[str, Any],
    *,
    head: str | None = None,
    approve: bool = False,
    mapper: DiffMapper | None = None,
    merge_base: str | None = MB,
) -> tuple[ReviewSource, Doc]:
    """What ``prepare`` builds: the source and the ``reanchored_draft``."""
    anchor = doc["anchor"]
    pinned = head or anchor["head_sha"]
    done = reanchor(
        Draft(copy.deepcopy(dict(doc)), "0" * 64),
        head_sha=pinned,
        base_ref=anchor["base_ref"],
        base_sha=anchor["base_sha"],
        merge_base_sha=merge_base,
        mapper=mapper or DiffMapper.from_text(""),
    )
    return review_source(doc, done, head_sha=pinned, approve=approve), done.draft
