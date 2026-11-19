# ruff: noqa: E501  (prose rows that become docs/live-checklist.md)
"""The checklist of every host behavior that was verified only against fakes or by reading source,
each mapped to a live scenario or test, or to an explicit manual step.

``docs/live-checklist.md`` is generated from this module
(``uv run python -m tests.live.checklist --write``) and a test checks that it is current, that every
row has an automated check or a manual step, and that no scenario names a row that does not exist.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs" / "live-checklist.md"
LOCAL = "tests/live/test_live_local.py"


@dataclass(frozen=True)
class Row:
    id: str
    source: str
    text: str
    tests: tuple[str, ...] = ()  # test functions (not scenarios) that settle it
    manual: str = ""  # what a human does, alone or for the part no test can reach


def _r(id: str, source: str, text: str, *tests: str, manual: str = "") -> Row:
    return Row(id, source, text, tests, manual)


ROWS: tuple[Row, ...] = (
    # forgectl gateway (#8, PR 57)
    _r(
        "gw.flag_forms",
        "#8",
        "forgectl's parser accepts `--flag=value` for every flag the gateway sends (`--timeout=`, `--config=`, `--dedupe-key=` and the rest).",
    ),
    _r(
        "gw.version_format",
        "#8",
        "The `forgectl --version` output format (recorded, not asserted).",
    ),
    _r(
        "gw.signal_term",
        "#8",
        "Exit code and output of forgectl when a signal reaches pr-comment before and after a write (pr-comment forwards it to the exact child).",
    ),
    _r(
        "gw.deadline_kill",
        "#8, #9",
        "Exit code and output of forgectl killed by SIGTERM at the deadline, and the output cap.",
        manual="The 32 MiB output cap cannot be forced on a real host: read a PR with a very large diff and a very large comment history and confirm exit 12 `lower_contract_mismatch` for output over the cap.",
    ),
    _r(
        "gw.hook_outlasts_slack",
        "#8",
        "A forgectl hook that outlasts the deadline slack is killed and the write reported `outcome_unknown`.",
    ),
    _r(
        "gw.internal_after_landed",
        "#8",
        "Whether exit 1 `internal` can follow a landed write.",
        manual="Cannot be provoked on demand. Keep each run's report: if a write ever ends in exit 1, check whether it landed and record it here.",
    ),
    _r(
        "gw.audit_failed_outcome",
        "#8",
        "The real `details.outcome` values of forgectl's `audit_failed`.",
        manual="Make forgectl's state directory unwritable (a read-only mount) after its first write attempt of a throwaway run and read the `audit_failed` envelope; record `details.outcome`.",
    ),
    _r(
        "gw.outcome_partial_landed",
        "#8",
        "The `landed` shape of `outcome_partial` on GitHub and GitLab.",
    ),
    _r(
        "gw.wrong_host_response_landed",
        "#8",
        "Whether `wrong_host_response` can follow a landed write.",
        manual="Cannot be provoked without redirecting a host. Observe opportunistically and record.",
    ),
    _r(
        "gw.early_exit_stdin",
        "#8",
        "forgectl exits early without reading stdin (`body_too_large` over 16 MiB): pr-comment ends with forgectl's status.",
    ),
    _r(
        "gw.dry_run_already_posted",
        "#8",
        "A dry-run post on GitHub and GitLab reports `already_posted` or a dedupe answer.",
    ),
    _r(
        "gw.stdout_strict_json",
        "#8",
        "Stdout is strict JSON when forgectl prints warnings (every step of every scenario parses stdout and fails on anything else).",
    ),
    _r(
        "gw.leftover_drafts_malformed",
        "#8",
        "A malformed `host_rejected` `details.leftover_drafts` and what a caller should do.",
        manual="Needs a host that rejects a review and leaves drafts; record the real shape. Until then pr-comment's `lower_contract_mismatch` stands.",
    ),
    _r(
        "gw.macos_fallback",
        "#8, #40",
        "The no-`waitid` fallback in `gateway.py` on macOS.",
        manual="On a Mac run `make verify`, then the signal scenario (`signal_mid_write`) in run mode, and record the result in the release notes of the version that declares macOS support.",
    ),
    # the fake (#9, PR 65) and its fidelity (#26, PR 68)
    _r(
        "fk.confirmation_order",
        "#9",
        "Confirmation, hooks and write errors after host reads: exit 13 after the reads, a refusal such as `stale_head` winning over confirmation, and the `would_refuse` fields on a dry run.",
    ),
    _r(
        "fk.signal_after_write",
        "#9",
        "A signal after a write was attempted: exit 12 `outcome_unknown` with an envelope, then one post after the rerun (the before-write case is in the conformance replay).",
    ),
    _r(
        "fk.body_vectors",
        "#9",
        "The `body_sha256`, `body_bytes`, marker and neutralizing computations over more than one vector (the stored text is compared with pr-comment's own plan).",
    ),
    _r("fk.plan_event_null_approve", "#9", "`plan.event` is null for approve."),
    _r(
        "fk.gitlab_no_body_marker",
        "#9",
        "pr-comment's own marker placement for a GitLab review with no body: the marker is on the inline comment as stored (forgectl's rule for a body-less review is not probed separately).",
    ),
    _r(
        "fk.dedupe_hit",
        "#9",
        "Dedupe-hit semantics: `already_posted`, `follow_up_done`, `ids` and `review_id` on a hit, and GitHub `outcome_partial` on a hit that still needs the state step.",
    ),
    _r(
        "fk.codes_reasons",
        "#9",
        "Per-command allowed error codes and reasons, and the required details per reason (`stale_head`, `stale_base`, `not_found`, `not_open`).",
        manual="`self_approval`, `drafts_present`, `pending_review_exists`, `unknown_host` and `too_large` need a PR authored by the account, a leftover draft, an undeclared host and a very large PR: trigger each once by hand and record the envelope.",
    ),
    _r(
        "fk.id_prefixes",
        "#9",
        "Id prefixes per backend: GitHub `thread_ids` empty, GitLab `review_id` null, the ids of a comment, review and thread.",
    ),
    _r(
        "fk.whoami", "#9", "`whoami` partial results (exit 12), the host set and the warning lines."
    ),
    _r(
        "fk.config_flag",
        "#9",
        "`--config`: a directory, an unparsable file, and hosts or hooks content.",
    ),
    _r(
        "fk.enterprise_hosts",
        "#9",
        "GitHub Enterprise and self-hosted GitLab hosts.",
        manual="Set `PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR` and `PR_COMMENT_LIVE_GITLAB_SELFHOSTED_PR`; every scenario then runs on those targets too. Declare the host in forgectl's config (`PR_COMMENT_LIVE_FORGECTL_CONFIG`).",
    ),
    _r(
        "fk.rename_canonical_show",
        "#26",
        "After a repo rename, real forgectl answers `show` with the canonical new name (the fake answers with the old one).",
        manual="Rename a throwaway repo, run `forgectl show` with the old name, and compare `repo` with the new name; then fix `_same_pr` in `tests/fake_forgectl.py`.",
    ),
    _r(
        "fk.approve_interactive",
        "#9",
        "A human approve on a pty against a throwaway PR: declined once (nothing written), then confirmed.",
    ),
    # stdin contract (#32, PR 72)
    _r(
        "sc.post_bytes",
        "#32",
        "The bytes forgectl posts after `tool_missing`: the stored text is the body pr-comment handed over.",
    ),
    _r(
        "sc.yes_dedupe",
        "#32",
        "`--yes` confirmation and the dedupe-key behavior against a live host.",
    ),
    _r(
        "sc.early_exit_post_path",
        "#32",
        "Early exit without reading stdin through the post path: pr-comment's exit code and envelope equal forgectl's status.",
    ),
    _r(
        "sc.sigpipe",
        "#32",
        "The early exit relies on SIGPIPE being ignored; the post path must not reset it.",
    ),
    _r(
        "sc.versions",
        "#32",
        "forgectl versions other than the pinned 0.55.2 (the run records the version it saw).",
        manual="Run the suite once per forgectl 0.55.x release (the supported range, docs/interface.md) and keep the reports.",
    ),
    _r(
        "sc.macos_pipes",
        "#32",
        "Pipe buffer sizes on macOS.",
        manual="On a Mac run `early_exit_stdin` and the offline stdin contract test.",
    ),
    # threads (#35, #36)
    _r(
        "th.not_found",
        "#35, #36",
        "A thread id that is not on the PR gives exit 10 `not_found` (pr-comment's own handling of the answer; no raw forgectl call probes forgectl's not_found separately).",
    ),
    _r(
        "th.changed_false",
        "#35",
        "A no-op on a thread reports `changed: false` (pr-comment's own result; forgectl's own answer for a repeated resolve is not probed separately).",
    ),
    _r(
        "th.gl_d_ids",
        "#35",
        "A GitLab `gl:d:` discussion id resolves the same way as a `gh:rt:` id.",
    ),
    _r(
        "th.gitlab_body_thread",
        "#36",
        "The GitLab review body thread is created and resolved by default.",
    ),
    _r(
        "th.thread_author_is_self",
        "#36",
        "`thread_author_is_self` as forgectl reports it for a thread the account started.",
    ),
    _r(
        "th.complete_false",
        "#36",
        "A `comments` listing with `complete: false`.",
        manual="Needs a PR with more review threads than one host page holds; resolve one thread of it and confirm `changed: null` for an unlisted thread.",
    ),
    _r(
        "th.race_changed_false",
        "#36",
        "A `resolve` answer with `changed: false` after someone else resolved the thread between the read and the write.",
        manual="Resolve a thread in the web UI while a `resolve` run waits at its dialog (run it interactively) and confirm no post-post hook runs and no audit write is recorded.",
    ),
    _r("th.reply_threaded", "#9", "The reply `threaded` rule per target kind."),
    # re-anchor and stale heads
    _r(
        "st.stale_head",
        "#9",
        "A draft written at an older head is refused `stale_head` without `--reanchor-to`.",
    ),
    _r(
        "st.reanchor",
        "#9",
        "A re-anchored post after a push, and its rerun reporting `already_posted`.",
    ),
    _r(
        "bs.base_ref_forms",
        "#37",
        "A real PR's base ref forms are accepted by `draft new` and `draft check`.",
    ),
    _r(
        "fk.diff_markers",
        "#9",
        "forgectl's `diff` keeps a carriage return and a `\\ No newline at end of file` marker (recorded, not asserted).",
        manual="Put a CRLF file and a file without a trailing newline in the throwaway PR; the `diff_markers` scenario records the counts it finds.",
    ),
    _r(
        "fk.since_grammar",
        "#9",
        "The grammar of `--since` on a read (awaiting a maintainer decision: recorded by hand only, no assertion).",
        manual="Run `forgectl comments --since` with an RFC 3339 time, a date and a relative form against a throwaway PR and record which are accepted; nothing is asserted until the maintainer decides.",
    ),
    # hooks (#33, #82)
    _r(
        "hk.decision_ids",
        "#33",
        "A decision id printed by a hook reaches forgectl as `--decision-id=`.",
    ),
    _r(
        "hk.scrubbed_env",
        "#33",
        "A hook's environment holds none of forgectl's ambient token variables: a canary one is set for the run and must be absent, and `hook_env_deny` applies.",
    ),
    _r(
        "hk.group_kill",
        "#33",
        "A hook that times out is vetoed and its process group, background processes included, is killed.",
    ),
    _r(
        "hk.async",
        "#33, #82",
        "An async `post-post` hook (a 20 second sleep) is not waited for and its working directory is swept after a day only when empty (OPEN DECISION: async hooks, #82; the marker is recorded, nothing asserted about it).",
        manual="Leave an async hook's working directory in the state `tmp/` directory, age it by a day (`touch -d`) and run any command: confirm it is removed only when empty.",
    ),
    # escalation (#34)
    _r(
        "eo.forgectl_scope",
        "#34",
        "A write that forgectl escalates (exit 13) fails without the flag and succeeds with `--override-escalation=forgectl`; how forgectl audits it.",
        manual="Read forgectl's audit record of the overridden write (`forgectl export`) and confirm it names the escalation.",
    ),
    # text safety (#19, #20, #22)
    _r(
        "ts.fence_255",
        "#19",
        "GitHub's and GitLab's own engines share the 255 fence-length limit: 254, 255, 256 and 300 backtick and tilde runs, and a suggestion with both kinds, are stored with the right fence and render as the same single code block as a short degraded control (the shape is also compared with an applicable control, recorded).",
    ),
    _r(
        "ts.run_split",
        "#19",
        "A degraded suggestion with backtick and tilde runs of 255 in one suggestion splits with the visible `<run split>` marker (and only then), and a backtick run of 255 or more gets a tilde fence of at most 255 characters.",
    ),
    _r(
        "ts.degraded_suggestion",
        "#19",
        "A degraded suggestion is posted inline as a text block and is not applicable.",
    ),
    _r(
        "ts.applicable_suggestion",
        "#22",
        "An applicable suggestion is stored with the host's own fence (`suggestion`, or GitLab's `suggestion:-N+0`).",
        manual="Open the inline comment in the web UI and confirm the Apply suggestion button is there.",
    ),
    _r(
        "ts.moved_finding",
        "#9",
        "A finding whose line is not in the diff moves to the review body.",
    ),
    _r(
        "ts.suggestion_infostring",
        "#22",
        "Whether either host treats a `suggestion` info string after the first word as a suggestion (recorded, not asserted; OPEN DECISION: a suggestion swallowed by an open fence).",
    ),
    _r(
        "ts.suggestion_longer_fence",
        "#22",
        "Whether either host applies a suggestion with a fence longer than three backticks (decides whether the `fence` degrade can be dropped; recorded, not asserted).",
    ),
    _r("ts.suggestion_variants", "#22", "Tilde and indented suggestion fences (recorded)."),
    _r(
        "ts.suggestion_fence_body",
        "#103",
        "A 4-backtick suggestion fence whose body holds a line of three backticks, or a `~~~` line, renders as one applicable suggestion with the body verbatim; the same body in a 3-backtick fence is the negative control. The two-line bodies are compared with a plain two-line suggestion (`control-suggestion-2line`), and the body is read from the suggestion's added rows or a code block (recorded, not asserted).",
    ),
    _r(
        "ts.suggestion_line_cell",
        "#103",
        "The line-number cell of a rendered suggestion against the line the comment is on: `match`, `mismatch`, `hidden` (dots or empty, as GitHub showed for tilde fences), `hidden-class+match` or `hidden-class+mismatch` (the cell shows a real number but carries the hidden class, so the two signals disagree; the first live run decides which of these tilde fences produce, so any of `match`, `mismatch`, `hidden`, `hidden-class+match`, `hidden-class+mismatch` or `absent` may be recorded) or `absent`. Every form classed `applicable` or `other` is probed. A tilde suggestion is not proven applicable by its widget alone (recorded, not asserted).",
    ),
    _r(
        "ts.mention_filters",
        "#22",
        "Neutral mentions, `a@user` and `1@user` do not link on either host; a literal mention does (the positive control).",
    ),
    _r(
        "ts.fullwidth_fold",
        "#22",
        "Whether either host folds fullwidth `＠`, `＜` or `！` to ASCII (recorded: what is stored, and the image and bold tags of the render; OPEN DECISION: fullwidth `＠`).",
    ),
    _r(
        "ts.entities_unterminated",
        "#22",
        "Whether either host decodes unterminated entities before the mention filter.",
    ),
    _r("ts.gitlab_mentions", "#22", "GitLab: `@all` and group forms do not link."),
    _r(
        "ts.gitlab_engine",
        "#22",
        "GitLab's own Markdown (`>>>` quote, `[[_TOC_]]`, math, quick actions, references) behaves as pr-comment assumes; a slash line is not run as a quick action.",
    ),
    _r(
        "ts.github_media",
        "#22",
        "A bare attachment URL on its own line renders as media on GitHub (recorded; `counts.images` does not count it).",
    ),
    _r(
        "ts.github_diagrams",
        "#22",
        "Diagram fences (`mermaid`, `geojson`, `stl`) render on GitHub (recorded).",
    ),
    _r(
        "ts.link_forms",
        "#22",
        "How a `javascript:` or relative link renders on the hosts (unverified; the pinned cmark-gfm renders an empty href) (recorded: the `javascript:` hrefs found).",
    ),
    _r(
        "ts.postprocess",
        "#22",
        "Host post-processing after the engine: sanitizing, emoji, issue and commit links, `@` inside a link text (recorded).",
    ),
    _r("ts.marker_display", "#22", "How a host shows the hidden marker comment (not as text)."),
    _r(
        "ts.engine_version",
        "#22",
        "Whether GitHub's engine is the pinned `cmarkgfm` version at all.",
        manual="Take the pinned fuzz corpus's disagreement cases (`tests/test_text_safety_fuzz.py`), post them as comments and compare the rendered HTML with `cmarkgfm`'s.",
    ),
    _r(
        "ts.line_separators",
        "#22",
        "Whether hosts treat U+2028, U+2029, VT, FF and NEL as line breaks (observe: recorded, not asserted). cmark does not, and the first live run on GitHub did not match that expectation: the probe came back as line breaks (one `p` and six `br` in the rendered HTML), so the cmark-based expectation of no break was not observed to hold. The run did not separate the five characters, and forgectl itself splits on these characters and rejoins with LF (its neutralizing step 1), so the break may come from forgectl and not from the host: which of the two does it, and for which character, is still open. GitLab was not run.",
        manual="Read the stored text of the probe comment (does it still hold the separator characters, or LF?) and, for each of the five characters alone between two words, the rendered HTML, to tell the host's doing from forgectl's.",
    ),
    _r(
        "ts.dedupe_line_format",
        "#20",
        "The dedupe line format `tests/textsafety.py` hard-codes, and how forgectl joins it to the body.",
    ),
    _r(
        "ts.stdin_trailing_newline",
        "#20",
        "A trailing newline on stdin does not change the stored text or `body_sha256`.",
    ),
    _r(
        "ts.unicode_whitespace_marker",
        "#20",
        "Whether forgectl treats Unicode whitespace (NBSP) as empty when it looks for the last non-empty line.",
    ),
    _r(
        "ts.host_keeps_bytes",
        "#20",
        "The hosts keep the marker comment bytes and trailing whitespace verbatim.",
    ),
    _r(
        "ts.neutralizing_equal",
        "#20",
        "forgectl's neutralizing leaves pr-comment's output unchanged (the `neutralizing_mismatch` warning path never fires).",
        manual="The warning path itself needs forgectl to change a body: post a body with a `forgectl:dedupe:` line in a code block through `forgectl comment` directly and compare.",
    ),
    # export (#38)
    _r(
        "ex.closed_pipe",
        "#38",
        "A closed stdout pipe with a real pager is silent and exits 0.",
        f"{LOCAL}::test_a_closed_pipe_with_head_ends_quietly_with_exit_zero",
        f"{LOCAL}::test_a_closed_pipe_with_less_on_a_terminal_ends_quietly",
    ),
    _r(
        "ex.unsafe_files",
        "#38",
        "The unsafe-file refusals (symlink, FIFO, directory, hard link, group-writable, foreign owner) on a real filesystem.",
        f"{LOCAL}::test_unsafe_audit_files_are_refused_on_a_real_filesystem",
        manual="The foreign-owner case needs root (it is skipped otherwise): run it once with `sudo`, or `chown` a copy of `audit.jsonl` to another user in a scratch state directory and confirm exit 12 `audit_unavailable`.",
    ),
)

ROW_IDS = {row.id for row in ROWS}

ENV_VARS: tuple[tuple[str, str], ...] = (
    (
        "PR_COMMENT_LIVE",
        "`1` opts in. Without it every host-facing test is skipped with its reason.",
    ),
    ("PR_COMMENT_LIVE_MODE", "`plan` (default), `preflight` or `run`."),
    (
        "PR_COMMENT_LIVE_ALLOW",
        "Comma-separated repo ids (`host/path`, exact, lowercase, no wildcard). Required beyond plan mode. A target outside it is refused before any call and again against forgectl's canonical repo.",
    ),
    (
        "PR_COMMENT_LIVE_GITHUB_PR",
        "The throwaway GitHub PR (a URL or `github.com/<owner>/<repo>#N`). It must add lines to at least one file.",
    ),
    (
        "PR_COMMENT_LIVE_GITLAB_PR",
        "The throwaway GitLab MR (a URL or `gitlab.com/<group>/<project>#N`).",
    ),
    (
        "PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR",
        "Optional: a PR on a GitHub host other than github.com.",
    ),
    (
        "PR_COMMENT_LIVE_GITLAB_SELFHOSTED_PR",
        "Optional: an MR on a GitLab host other than gitlab.com.",
    ),
    (
        "PR_COMMENT_LIVE_<KEY>_OLD_HEAD",
        "Optional, per target: a 40-hex earlier head of that PR (for the stale-head and re-anchor scenario).",
    ),
    (
        "PR_COMMENT_LIVE_<KEY>_CLOSED_PR",
        "Optional, per target: a closed or merged throwaway PR (for the `not_open` scenario).",
    ),
    (
        "PR_COMMENT_LIVE_<KEY>_APPROVE_PR",
        "Optional, per target: a throwaway PR the account did not author (for the interactive approve).",
    ),
    ("PR_COMMENT_LIVE_APPROVE", "`1` allows the interactive approve scenario."),
    (
        "PR_COMMENT_LIVE_SLOW",
        "`1` allows scenarios that take more than a minute (the deadline kill).",
    ),
    (
        "PR_COMMENT_LIVE_REPORT_DIR",
        "Absolute directory (no `..`) for the reports and the ledger of what was posted. Required in run mode; it must be creatable and writable, and is created and probed before the first write.",
    ),
    (
        "PR_COMMENT_LIVE_RENDER",
        "`1` reads rendered HTML through `gh` or `glab` (GraphQL, read only; they need credentials in environment variables) so the rendering rows are observed; without it they are recorded as unavailable and become manual steps.",
    ),
    (
        "PR_COMMENT_LIVE_CLEANUP_CMD",
        "Optional absolute path of one existing executable file (no arguments) called as `<cmd> <pr> <forgectl id>` for every id the run posted, with the same private environment as the other children. It replaces the default cleanup (which deletes general and inline comments on allowlisted repos through `gh` or `glab` and lists the rest).",
    ),
    (
        "PR_COMMENT_LIVE_KEEP",
        "`1` skips every cleanup, the cleanup command included: what the run posted stays on the host and is listed as left. `0` or unset cleans up; any other value is refused.",
    ),
    (
        "PRCLIVE_TOKEN_<KEY>",
        "Not read by the suite: the name to give a forgectl `token_env` (in the config of `PR_COMMENT_LIVE_FORGECTL_CONFIG`) for the target `<KEY>`. It must lie outside the `PR_COMMENT_` prefix, because every `PR_COMMENT_*` variable is dropped from the children's environment; the suite refuses a token variable under that prefix by name.",
    ),
    ("PR_COMMENT_LIVE_FORGECTL", "forgectl binary name or path (default `forgectl`)."),
    (
        "PR_COMMENT_LIVE_FORGECTL_CONFIG",
        "Optional forgectl config file passed with `--config` (declares enterprise hosts). When set, the environment's `HOME` and `XDG_CONFIG_HOME` are passed on; when unset they are private and credentials must come from environment variables.",
    ),
    (
        "PR_COMMENT_LIVE_GITLAB_GROUP",
        "Optional: a GitLab group path used for the group-mention probe.",
    ),
    (
        "PR_COMMENT_LIVE_FS_DIR",
        "Optional: a directory on the filesystem the local export checks should use.",
    ),
    (
        "PR_COMMENT_LIVE_RUN_ID",
        "Optional: 6 to 24 characters of a-z and 0-9. A run id fixes every draft id and dedupe key; one whose ledger or run-mode results exist in the report directory is refused.",
    ),
)


def _scenarios() -> list[tuple[str, str, str, str, str]]:
    from .scenarios import SCENARIOS

    found = []
    for sc in SCENARIOS.values():
        needs = ", ".join(sc.needs) or "-"
        kind = "writes" if sc.writes else "reads"
        found.append((sc.id, "/".join(sc.backends), kind, needs, sc.doc))
    return found


def automated(row_id: str) -> list[str]:
    from .scenarios import SCENARIOS

    names = [f"scenario `{sc.id}`" for sc in SCENARIOS.values() if row_id in sc.rows]
    row = next(r for r in ROWS if r.id == row_id)
    names += [f"test `{t}`" for t in row.tests]
    return names


def render() -> str:
    out: list[str] = []
    w = out.append
    w("# Live suite and checklist")
    w("")
    w(
        "This file is generated from `tests/live/checklist.py` (`python -m tests.live.checklist --write`); a test fails when it is stale. Do not edit it by hand."
    )
    w("")
    w(
        "The live suite re-checks, against real hosts, every behavior that was verified only against the fake forgectl or by reading source (issue #39). It is run by the maintainer only: it uses real accounts, can write to real PRs and MRs, and may be billed. It is deselected by default (`-m 'not live'`) and never runs in CI. The existing replay tests of the real forgectl (`tests/test_fake_forgectl_conformance.py`, `tests/test_forgectl_stdin_contract.py`) and the two local checks in `tests/live/test_live_local.py` need no host and no opt-in."
    )
    w("")
    w("## Safety model")
    w("")
    w(
        "- Opt-in: every host-facing test is skipped unless `PR_COMMENT_LIVE=1`. With the opt-in, a setting the suite refuses fails the run before any call, naming variables and never values."
    )
    w(
        "- Allowlist: `PR_COMMENT_LIVE_ALLOW` lists the exact repo ids the suite may write to. Each target is checked as written, and again against the canonical repo forgectl reports for it, before any write. A write whose argv names no verified target never runs."
    )
    w(
        "- Modes: `plan` (the default) calls nothing and prints every step; `preflight` runs reads and turns every write into a `--dry-run`; `run` writes. Steps that need an id only a real write produces are shown as `<name>` and are deferred in preflight; in `run` mode a step still holding such a placeholder fails the scenario. In plan mode a configured target must be on the allowlist like any other (a target with no `PR_COMMENT_LIVE_ALLOW` is a refused setting); only a plan with no target at all shows steps against placeholder repos, which need no allowlist because nothing is called."
    )
    w(
        "- Isolation: pr-comment runs with its own config file, state directory and working directory, and a tap that logs each forgectl argv. Every `PR_COMMENT_*` variable is dropped, so a forgectl `token_env` must name a variable outside that prefix: use `PRCLIVE_TOKEN_<KEY>` (`<KEY>` as under Variables, for example `PRCLIVE_TOKEN_GITHUB`), and a token variable under `PR_COMMENT_` is refused by name. The rest of the environment reaches forgectl for its credentials and is never printed (secret-looking values of eight characters or more are redacted from every message, report and note). Unless `PR_COMMENT_LIVE_FORGECTL_CONFIG` is set, every child also gets a private `HOME` and `XDG_CONFIG_HOME`, so the developer's own `gh` or `glab` login is not found: forgectl's credentials must come from environment variables (for example `GH_TOKEN` or `GITLAB_TOKEN`, or the `PRCLIVE_TOKEN_<KEY>` a `token_env` in your forgectl config names) or from that config file. Each child runs in a process group of its own; on a timeout or an interrupt that exact group is killed."
    )
    w(
        "- Cleanup: every body carries the run's tag (`live-<run id>`) so what a run posted can be found. The ids it posted are written to `ledger-<run id>.jsonl` in the report directory. A run leaves nothing behind by default: at the end of each scenario every id in the ledger is cleaned up, by `PR_COMMENT_LIVE_CLEANUP_CMD` when set, else by the default cleanup, which deletes the general comments and inline review comments (GitLab notes too) of a PR on the allowlist through `gh api graphql` or `glab api` (so `gh` or `glab` must be on `PATH`) and writes a `deleted` row to the ledger for each. Each delete call runs with the `PRCLIVE_TOKEN_<KEY>` of the target the PR belongs to, passed as `GH_TOKEN` (GitHub) or `GITLAB_TOKEN` (GitLab), and also as `GH_ENTERPRISE_TOKEN` for a GitHub host other than github.com, because `gh` reads that variable there; when no such variable is set the rest of the environment is left as it is, so a token you already export under those names is used. `GH_HOST` and `GH_REPO` are removed from that call and `--hostname` is always passed. A GitHub delete counts only when the answer is one JSON object with no `errors` and an object under the mutation's name; anything else is listed as left. Only ids this run's own parsed answers reported are deleted: an id read from output that was not one JSON document, or reported by an answer that says `already_posted` and was not posted earlier in this run, is listed as left and never deleted. GitHub ids must be plain node ids and GitLab note ids digits, or they are listed as left. On GitLab only notes (`gl:n:` ids) are deleted: inline comments are discussions (`gl:d:` ids), which the suite leaves for you to delete by hand, and the DELETE route for a note is not verified live (it follows the documented REST route and is judged by the exit code alone). `PR_COMMENT_LIVE_KEEP=1` leaves everything for you to look at on the host, even when `PR_COMMENT_LIVE_CLEANUP_CMD` is set. The expectation for a run against a real repo: run only against a throwaway repo (an archived one is fine afterwards), because what hosts cannot delete stays, and the notes at the end list each such item as left for a human: submitted reviews and approvals, review threads, and a thread the suite resolved. Anything the cleanup could not delete (no `gh`, a failed call, a repo off the allowlist) is listed too. A write whose answer could not be read is listed as possibly posted (the entry is written before the write starts and kept after a timeout or an interrupt). The cleanup command runs under the same private `HOME` as every other child, so it must authenticate through environment variables or `PR_COMMENT_LIVE_FORGECTL_CONFIG`, never through a stored `gh` or `glab` login in the real home. A cleanup command that is missing or times out is a note, never a failure that hides the scenario's own."
    )
    w(
        "- Idempotency: a run id fixes every draft id and dedupe key within one run (the dedupe and signal scenarios rely on it). Scenarios look only for comments with their own tag. A run id whose ledger or run-mode results already exist in the report directory is refused, so a second run cannot repeat the first one's writes by accident: use another run id or another report directory."
    )
    w(
        "- Raw probes: a few scenarios post through `forgectl` directly (a literal mention as a positive control, suggestion fences pr-comment refuses, a body that ends in a no-break space). They are marked `raw` in the step list and run only on an allowlisted target."
    )
    w(
        "- Never run the suite with `-l` or `--showlocals`: a failing frame can hold a raw response. The suite hides its own frames from tracebacks, and its failure messages are redacted, but pytest's local display is not."
    )
    w(
        "- What the suite never does: print an environment variable, post outside the allowlist, change a PR's branch or state, or send a quick action that changes anything (the GitLab probe sends `/shrug`, which only appends a text)."
    )
    w("")
    w("## Running it")
    w("")
    w(
        "1. Create throwaway repositories (and, for GitLab, a throwaway project) that nothing else uses. Open a PR or MR in each that adds at least a dozen lines to a file. For the stale-head scenario push one more commit and note the earlier head."
    )
    w(
        "2. List the repos in `PR_COMMENT_LIVE_ALLOW`, set the target variables, and look at the plan: `PR_COMMENT_LIVE=1 PR_COMMENT_LIVE_MODE=plan uv run pytest -m live tests/live/test_live_hosts.py -rs`."
    )
    w(
        "3. Run `preflight` (reads only) once, then `run` with `PR_COMMENT_LIVE_REPORT_DIR` set. The report directory then holds one `results-*.json` per scenario and target."
    )
    w(
        "4. Paste the observed facts into design.md's Live verification table through a docs PR. The suite decides none of the open user decisions; rows marked OPEN DECISION are recorded only."
    )
    w("")
    w("## Variables")
    w("")
    w("| Variable | Meaning |")
    w("|---|---|")
    for name, text in ENV_VARS:
        w(f"| `{name}` | {text} |")
    w("")
    w("`<KEY>` is one of `GITHUB`, `GITLAB`, `GITHUB_ENTERPRISE`, `GITLAB_SELFHOSTED`.")
    w("")
    w("## Scenarios")
    w("")
    w("| Scenario | Host | Kind | Needs | What it does |")
    w("|---|---|---|---|---|")
    for name, hosts, kind, needs, doc in _scenarios():
        w(f"| `{name}` | {hosts} | {kind} | {needs} | {doc} |")
    w("")
    w("## Still-to-verify rows")
    w("")
    w(
        "Each row is settled by a scenario or test, by a manual step, or both. Source is the issue whose note listed it."
    )
    w("")
    w("| Row | Source | Behavior | Automated by | Manual step |")
    w("|---|---|---|---|---|")
    for row in ROWS:
        by = "<br>".join(automated(row.id)) or "-"
        w(f"| `{row.id}` | {row.source} | {row.text} | {by} | {row.manual or '-'} |")
    w("")
    w("## Not run by the author")
    w("")
    w(
        "The host-facing scenarios were written and exercised offline only, in `plan` mode and against the pinned fake forgectl in `preflight` and `run` mode. No scenario has run against a real host, so every assertion about a host's answer is a prediction from the documents until the maintainer runs the suite once and corrects what it finds."
    )
    w("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    text = render()
    if "--write" in argv:
        DOC.write_text(text, encoding="utf-8")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
