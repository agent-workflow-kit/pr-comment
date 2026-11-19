# Live suite and checklist

This file is generated from `tests/live/checklist.py` (`python -m tests.live.checklist --write`); a test fails when it is stale. Do not edit it by hand.

The live suite re-checks, against real hosts, every behavior that was verified only against the fake forgectl or by reading source (issue #39). It is run by the maintainer only: it uses real accounts, can write to real PRs and MRs, and may be billed. It is deselected by default (`-m 'not live'`) and never runs in CI. The existing replay tests of the real forgectl (`tests/test_fake_forgectl_conformance.py`, `tests/test_forgectl_stdin_contract.py`) and the two local checks in `tests/live/test_live_local.py` need no host and no opt-in.

## Safety model

- Opt-in: every host-facing test is skipped unless `PR_COMMENT_LIVE=1`. With the opt-in, a setting the suite refuses fails the run before any call, naming variables and never values.
- Allowlist: `PR_COMMENT_LIVE_ALLOW` lists the exact repo ids the suite may write to. Each target is checked as written, and again against the canonical repo forgectl reports for it, before any write. A write whose argv names no verified target never runs.
- Modes: `plan` (the default) calls nothing and prints every step; `preflight` runs reads and turns every write into a `--dry-run`; `run` writes. Steps that need an id only a real write produces are shown as `<name>` and are deferred in preflight; in `run` mode a step still holding such a placeholder fails the scenario. In plan mode a configured target must be on the allowlist like any other (a target with no `PR_COMMENT_LIVE_ALLOW` is a refused setting); only a plan with no target at all shows steps against placeholder repos, which need no allowlist because nothing is called.
- Isolation: pr-comment runs with its own config file, state directory and working directory, and a tap that logs each forgectl argv. Every `PR_COMMENT_*` variable is dropped; the rest of the environment reaches forgectl for its credentials and is never printed (secret-looking values of eight characters or more are redacted from every message, report and note). Unless `PR_COMMENT_LIVE_FORGECTL_CONFIG` is set, every child also gets a private `HOME` and `XDG_CONFIG_HOME`, so the developer's own `gh` or `glab` login is not found: forgectl's credentials must come from environment variables (for example `GH_TOKEN` or `GITLAB_TOKEN`) or from that config file. Each child runs in a process group of its own; on a timeout or an interrupt that exact group is killed.
- Cleanup: every body carries the run's tag (`live-<run id>`) so what a run posted can be found. The ids it posted are written to `ledger-<run id>.jsonl` in the report directory and passed to `PR_COMMENT_LIVE_CLEANUP_CMD` when set. A write whose answer could not be read is listed as possibly posted (the entry is written before the write starts and kept after a timeout or an interrupt). Hosts cannot delete a submitted review or an approval, and a thread the suite resolved stays resolved; those are listed as left for a human. A cleanup command that is missing or times out is a note, never a failure that hides the scenario's own.
- Idempotency: a run id fixes every draft id and dedupe key within one run (the dedupe and signal scenarios rely on it). Scenarios look only for comments with their own tag. A run id whose ledger or run-mode results already exist in the report directory is refused, so a second run cannot repeat the first one's writes by accident: use another run id or another report directory.
- Raw probes: a few scenarios post through `forgectl` directly (a literal mention as a positive control, suggestion fences pr-comment refuses, a body that ends in a no-break space). They are marked `raw` in the step list and run only on an allowlisted target.
- Never run the suite with `-l` or `--showlocals`: a failing frame can hold a raw response. The suite hides its own frames from tracebacks, and its failure messages are redacted, but pytest's local display is not.
- What the suite never does: print an environment variable, post outside the allowlist, change a PR's branch or state, or send a quick action that changes anything (the GitLab probe sends `/shrug`, which only appends a text).

## Running it

1. Create throwaway repositories (and, for GitLab, a throwaway project) that nothing else uses. Open a PR or MR in each that adds at least a dozen lines to a file. For the stale-head scenario push one more commit and note the earlier head.
2. List the repos in `PR_COMMENT_LIVE_ALLOW`, set the target variables, and look at the plan: `PR_COMMENT_LIVE=1 PR_COMMENT_LIVE_MODE=plan uv run pytest -m live tests/live/test_live_hosts.py -rs`.
3. Run `preflight` (reads only) once, then `run` with `PR_COMMENT_LIVE_REPORT_DIR` set. The report directory then holds one `results-*.json` per scenario and target.
4. Paste the observed facts into design.md's Live verification table through a docs PR. The suite decides none of the open user decisions; rows marked OPEN DECISION are recorded only.

## Variables

| Variable | Meaning |
|---|---|
| `PR_COMMENT_LIVE` | `1` opts in. Without it every host-facing test is skipped with its reason. |
| `PR_COMMENT_LIVE_MODE` | `plan` (default), `preflight` or `run`. |
| `PR_COMMENT_LIVE_ALLOW` | Comma-separated repo ids (`host/path`, exact, lowercase, no wildcard). Required beyond plan mode. A target outside it is refused before any call and again against forgectl's canonical repo. |
| `PR_COMMENT_LIVE_GITHUB_PR` | The throwaway GitHub PR (a URL or `github.com/<owner>/<repo>#N`). It must add lines to at least one file. |
| `PR_COMMENT_LIVE_GITLAB_PR` | The throwaway GitLab MR (a URL or `gitlab.com/<group>/<project>#N`). |
| `PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR` | Optional: a PR on a GitHub host other than github.com. |
| `PR_COMMENT_LIVE_GITLAB_SELFHOSTED_PR` | Optional: an MR on a GitLab host other than gitlab.com. |
| `PR_COMMENT_LIVE_<KEY>_OLD_HEAD` | Optional, per target: a 40-hex earlier head of that PR (for the stale-head and re-anchor scenario). |
| `PR_COMMENT_LIVE_<KEY>_CLOSED_PR` | Optional, per target: a closed or merged throwaway PR (for the `not_open` scenario). |
| `PR_COMMENT_LIVE_<KEY>_APPROVE_PR` | Optional, per target: a throwaway PR the account did not author (for the interactive approve). |
| `PR_COMMENT_LIVE_APPROVE` | `1` allows the interactive approve scenario. |
| `PR_COMMENT_LIVE_SLOW` | `1` allows scenarios that take more than a minute (the deadline kill). |
| `PR_COMMENT_LIVE_REPORT_DIR` | Absolute directory (no `..`) for the reports and the ledger of what was posted. Required in run mode; it must be creatable and writable, and is created and probed before the first write. |
| `PR_COMMENT_LIVE_RENDER` | `1` reads rendered HTML through `gh` or `glab` (GraphQL, read only; they need credentials in environment variables) so the rendering rows are observed; without it they are recorded as unavailable and become manual steps. |
| `PR_COMMENT_LIVE_CLEANUP_CMD` | Optional absolute path of one existing executable file (no arguments) called as `<cmd> <pr> <forgectl id>` for every id the run posted, with the same private environment as the other children. Without it the ids are printed as left for a human. |
| `PR_COMMENT_LIVE_FORGECTL` | forgectl binary name or path (default `forgectl`). |
| `PR_COMMENT_LIVE_FORGECTL_CONFIG` | Optional forgectl config file passed with `--config` (declares enterprise hosts). When set, the environment's `HOME` and `XDG_CONFIG_HOME` are passed on; when unset they are private and credentials must come from environment variables. |
| `PR_COMMENT_LIVE_GITLAB_GROUP` | Optional: a GitLab group path used for the group-mention probe. |
| `PR_COMMENT_LIVE_FS_DIR` | Optional: a directory on the filesystem the local export checks should use. |
| `PR_COMMENT_LIVE_RUN_ID` | Optional: 6 to 24 characters of a-z and 0-9. A run id fixes every draft id and dedupe key; one whose ledger or run-mode results exist in the report directory is refused. |

`<KEY>` is one of `GITHUB`, `GITLAB`, `GITHUB_ENTERPRISE`, `GITLAB_SELFHOSTED`.

## Scenarios

| Scenario | Host | Kind | Needs | What it does |
|---|---|---|---|---|
| `forgectl_reads` | github/gitlab | reads | - | forgectl's version, whoami and show; the base ref form pr-comment copies into a draft. |
| `comment_post` | github/gitlab | writes | - | A comment: ids, the forms forwarded to forgectl, the marker and the author. |
| `dedupe` | github/gitlab | writes | - | The same comment twice: already_posted, the same ids, one comment; a new key posts again. |
| `body_roundtrip` | github/gitlab | writes | - | The stored text equals pr-comment's plan plus forgectl's dedupe line; newline handling. |
| `early_exit_stdin` | github/gitlab | reads | - | A review input forgectl refuses before reading all of stdin ends with its status. |
| `forgectl_confirmation_order` | github/gitlab | reads | - | forgectl without --yes: the dry run prints a plan, a refusal beats the exit 13 dialog. |
| `mentions` | github/gitlab | writes | - | Neutralized mentions stay inert on the host and the fullwidth characters are not folded. |
| `fence_limit` | github/gitlab | writes | - | Degraded suggestions with backtick and tilde runs of 254, 255, 256 and 300 characters. |
| `review_suggestions` | github/gitlab | writes | - | A review with an applicable suggestion, a degraded one and a moved finding. |
| `suggestion_probes` | github/gitlab | writes | - | Raw review comments: which fence forms a host treats as an applicable suggestion. |
| `render_probes` | github/gitlab | writes | - | How the host renders a media URL, diagram fences, odd links, emoji and the marker. |
| `gitlab_markdown` | gitlab | writes | - | GitLab's own Markdown: ``>>>`` quotes, [[_TOC_]], math, a slash line (a quick action). |
| `line_separators` | github/gitlab | writes | - | U+2028, U+2029, VT, FF and NEL in a body: stored as sent, and not line breaks on the host. |
| `gitlab_review_no_body` | gitlab | writes | - | A GitLab review whose body is only the label: review_id is null. |
| `gitlab_body_thread` | gitlab | writes | - | The GitLab body thread: resolved by default, then resolve (no-op), unresolve, resolve. |
| `resolve_unresolve` | github | writes | - | A GitHub review thread: resolve, resolve again, unresolve, and a thread not on the PR. |
| `reply_kinds` | github/gitlab | writes | - | Replies to a general comment, an inline thread and a review or note: the threaded rule. |
| `stale_and_reanchor` | github/gitlab | writes | old_head | A draft written at an older head: stale_head, then a re-anchored post. |
| `refusals` | github/gitlab | reads | - | Refusals that write nothing: stale head and base, an unknown reply target, a missing PR. |
| `closed_pr` | github/gitlab | writes | closed_ref | A closed or merged PR: review and approve are refused not_open, a comment still posts. |
| `approve_interactive` | github/gitlab | writes | approve | A human approval on a pty: declined once, then confirmed. Not undone by the suite. |
| `signal_mid_write` | github/gitlab | writes | - | SIGTERM, SIGHUP and SIGINT after the attempted record: outcome_unknown, one post. |
| `hooks_pass_through` | github/gitlab | writes | - | A pre-post hook's decision id reaches forgectl; its environment holds no secret. |
| `hooks_timeout_and_async` | github/gitlab | reads | - | A hook that times out is vetoed and its children die; an async hook is not waited for. |
| `override_escalation` | github/gitlab | writes | - | A forgectl hook that escalates (exit 13): refused, then allowed with the override. |
| `config_flag` | github/gitlab | reads | - | forgectl's --config: a directory or an unparsable file is config_invalid. |
| `deadline_kill` | github/gitlab | writes | slow | A forgectl hook that outlasts the deadline: outcome_unknown. |
| `dedupe_event_change` | github/gitlab | writes | - | The same draft id with another event: GitHub outcome_partial, GitLab completes it. |
| `marker_whitespace` | github/gitlab | writes | - | Where forgectl puts its dedupe line when the last line is a no-break space (raw probe). |
| `diff_markers` | github/gitlab | reads | - | What forgectl's diff keeps of a carriage return and of the no-newline marker (recorded). |

## Still-to-verify rows

Each row is settled by a scenario or test, by a manual step, or both. Source is the issue whose note listed it.

| Row | Source | Behavior | Automated by | Manual step |
|---|---|---|---|---|
| `gw.flag_forms` | #8 | forgectl's parser accepts `--flag=value` for every flag the gateway sends (`--timeout=`, `--config=`, `--dedupe-key=` and the rest). | scenario `comment_post` | - |
| `gw.version_format` | #8 | The `forgectl --version` output format (recorded, not asserted). | scenario `forgectl_reads` | - |
| `gw.signal_term` | #8 | Exit code and output of forgectl when a signal reaches pr-comment before and after a write (pr-comment forwards it to the exact child). | scenario `signal_mid_write` | - |
| `gw.deadline_kill` | #8, #9 | Exit code and output of forgectl killed by SIGTERM at the deadline, and the output cap. | scenario `deadline_kill` | The 32 MiB output cap cannot be forced on a real host: read a PR with a very large diff and a very large comment history and confirm exit 12 `lower_contract_mismatch` for output over the cap. |
| `gw.hook_outlasts_slack` | #8 | A forgectl hook that outlasts the deadline slack is killed and the write reported `outcome_unknown`. | scenario `deadline_kill` | - |
| `gw.internal_after_landed` | #8 | Whether exit 1 `internal` can follow a landed write. | - | Cannot be provoked on demand. Keep each run's report: if a write ever ends in exit 1, check whether it landed and record it here. |
| `gw.audit_failed_outcome` | #8 | The real `details.outcome` values of forgectl's `audit_failed`. | - | Make forgectl's state directory unwritable (a read-only mount) after its first write attempt of a throwaway run and read the `audit_failed` envelope; record `details.outcome`. |
| `gw.outcome_partial_landed` | #8 | The `landed` shape of `outcome_partial` on GitHub and GitLab. | scenario `dedupe_event_change` | - |
| `gw.wrong_host_response_landed` | #8 | Whether `wrong_host_response` can follow a landed write. | - | Cannot be provoked without redirecting a host. Observe opportunistically and record. |
| `gw.early_exit_stdin` | #8 | forgectl exits early without reading stdin (`body_too_large` over 16 MiB): pr-comment ends with forgectl's status. | scenario `early_exit_stdin` | - |
| `gw.dry_run_already_posted` | #8 | A dry-run post on GitHub and GitLab reports `already_posted` or a dedupe answer. | scenario `dedupe` | - |
| `gw.stdout_strict_json` | #8 | Stdout is strict JSON when forgectl prints warnings (every step of every scenario parses stdout and fails on anything else). | scenario `comment_post` | - |
| `gw.leftover_drafts_malformed` | #8 | A malformed `host_rejected` `details.leftover_drafts` and what a caller should do. | - | Needs a host that rejects a review and leaves drafts; record the real shape. Until then pr-comment's `lower_contract_mismatch` stands. |
| `gw.macos_fallback` | #8, #40 | The no-`waitid` fallback in `gateway.py` on macOS. | - | On a Mac run `make verify`, then the signal scenario (`signal_mid_write`) in run mode, and record the result in the release notes of the version that declares macOS support. |
| `fk.confirmation_order` | #9 | Confirmation, hooks and write errors after host reads: exit 13 after the reads, a refusal such as `stale_head` winning over confirmation, and the `would_refuse` fields on a dry run. | scenario `forgectl_confirmation_order` | - |
| `fk.signal_after_write` | #9 | A signal after a write was attempted: exit 12 `outcome_unknown` with an envelope, then one post after the rerun (the before-write case is in the conformance replay). | scenario `signal_mid_write` | - |
| `fk.body_vectors` | #9 | The `body_sha256`, `body_bytes`, marker and neutralizing computations over more than one vector (the stored text is compared with pr-comment's own plan). | scenario `body_roundtrip` | - |
| `fk.plan_event_null_approve` | #9 | `plan.event` is null for approve. | scenario `refusals` | - |
| `fk.gitlab_no_body_marker` | #9 | pr-comment's own marker placement for a GitLab review with no body: the marker is on the inline comment as stored (forgectl's rule for a body-less review is not probed separately). | scenario `gitlab_review_no_body` | - |
| `fk.dedupe_hit` | #9 | Dedupe-hit semantics: `already_posted`, `follow_up_done`, `ids` and `review_id` on a hit, and GitHub `outcome_partial` on a hit that still needs the state step. | scenario `dedupe`<br>scenario `dedupe_event_change` | - |
| `fk.codes_reasons` | #9 | Per-command allowed error codes and reasons, and the required details per reason (`stale_head`, `stale_base`, `not_found`, `not_open`). | scenario `refusals`<br>scenario `closed_pr` | `self_approval`, `drafts_present`, `pending_review_exists`, `unknown_host` and `too_large` need a PR authored by the account, a leftover draft, an undeclared host and a very large PR: trigger each once by hand and record the envelope. |
| `fk.id_prefixes` | #9 | Id prefixes per backend: GitHub `thread_ids` empty, GitLab `review_id` null, the ids of a comment, review and thread. | scenario `comment_post`<br>scenario `gitlab_review_no_body`<br>scenario `resolve_unresolve`<br>scenario `reply_kinds` | - |
| `fk.whoami` | #9 | `whoami` partial results (exit 12), the host set and the warning lines. | scenario `forgectl_reads` | - |
| `fk.config_flag` | #9 | `--config`: a directory, an unparsable file, and hosts or hooks content. | scenario `config_flag` | - |
| `fk.enterprise_hosts` | #9 | GitHub Enterprise and self-hosted GitLab hosts. | - | Set `PR_COMMENT_LIVE_GITHUB_ENTERPRISE_PR` and `PR_COMMENT_LIVE_GITLAB_SELFHOSTED_PR`; every scenario then runs on those targets too. Declare the host in forgectl's config (`PR_COMMENT_LIVE_FORGECTL_CONFIG`). |
| `fk.rename_canonical_show` | #26 | After a repo rename, real forgectl answers `show` with the canonical new name (the fake answers with the old one). | - | Rename a throwaway repo, run `forgectl show` with the old name, and compare `repo` with the new name; then fix `_same_pr` in `tests/fake_forgectl.py`. |
| `fk.approve_interactive` | #9 | A human approve on a pty against a throwaway PR: declined once (nothing written), then confirmed. | scenario `approve_interactive` | - |
| `sc.post_bytes` | #32 | The bytes forgectl posts after `tool_missing`: the stored text is the body pr-comment handed over. | scenario `comment_post` | - |
| `sc.yes_dedupe` | #32 | `--yes` confirmation and the dedupe-key behavior against a live host. | scenario `dedupe` | - |
| `sc.early_exit_post_path` | #32 | Early exit without reading stdin through the post path: pr-comment's exit code and envelope equal forgectl's status. | scenario `early_exit_stdin` | - |
| `sc.sigpipe` | #32 | The early exit relies on SIGPIPE being ignored; the post path must not reset it. | scenario `early_exit_stdin` | - |
| `sc.versions` | #32 | forgectl versions other than the pinned 0.55.2 (the run records the version it saw). | scenario `forgectl_reads` | Run the suite once per forgectl release the project supports (OPEN DECISION: the supported range) and keep the reports. |
| `sc.macos_pipes` | #32 | Pipe buffer sizes on macOS. | - | On a Mac run `early_exit_stdin` and the offline stdin contract test. |
| `th.not_found` | #35, #36 | A thread id that is not on the PR gives exit 10 `not_found` (pr-comment's own handling of the answer; no raw forgectl call probes forgectl's not_found separately). | scenario `gitlab_body_thread`<br>scenario `resolve_unresolve`<br>scenario `refusals` | - |
| `th.changed_false` | #35 | A no-op on a thread reports `changed: false` (pr-comment's own result; forgectl's own answer for a repeated resolve is not probed separately). | scenario `gitlab_body_thread`<br>scenario `resolve_unresolve` | - |
| `th.gl_d_ids` | #35 | A GitLab `gl:d:` discussion id resolves the same way as a `gh:rt:` id. | scenario `gitlab_body_thread` | - |
| `th.gitlab_body_thread` | #36 | The GitLab review body thread is created and resolved by default. | scenario `gitlab_body_thread` | - |
| `th.thread_author_is_self` | #36 | `thread_author_is_self` as forgectl reports it for a thread the account started. | scenario `resolve_unresolve` | - |
| `th.complete_false` | #36 | A `comments` listing with `complete: false`. | - | Needs a PR with more review threads than one host page holds; resolve one thread of it and confirm `changed: null` for an unlisted thread. |
| `th.race_changed_false` | #36 | A `resolve` answer with `changed: false` after someone else resolved the thread between the read and the write. | - | Resolve a thread in the web UI while a `resolve` run waits at its dialog (run it interactively) and confirm no post-post hook runs and no audit write is recorded. |
| `th.reply_threaded` | #9 | The reply `threaded` rule per target kind. | scenario `reply_kinds` | - |
| `st.stale_head` | #9 | A draft written at an older head is refused `stale_head` without `--reanchor-to`. | scenario `stale_and_reanchor` | - |
| `st.reanchor` | #9 | A re-anchored post after a push, and its rerun reporting `already_posted`. | scenario `stale_and_reanchor` | - |
| `bs.base_ref_forms` | #37 | A real PR's base ref forms are accepted by `draft new` and `draft check`. | scenario `forgectl_reads` | - |
| `fk.diff_markers` | #9 | forgectl's `diff` keeps a carriage return and a `\ No newline at end of file` marker (recorded, not asserted). | scenario `diff_markers` | Put a CRLF file and a file without a trailing newline in the throwaway PR; the `diff_markers` scenario records the counts it finds. |
| `fk.since_grammar` | #9 | The grammar of `--since` on a read (awaiting a maintainer decision: recorded by hand only, no assertion). | - | Run `forgectl comments --since` with an RFC 3339 time, a date and a relative form against a throwaway PR and record which are accepted; nothing is asserted until the maintainer decides. |
| `hk.decision_ids` | #33 | A decision id printed by a hook reaches forgectl as `--decision-id=`. | scenario `hooks_pass_through` | - |
| `hk.scrubbed_env` | #33 | A hook's environment holds none of forgectl's ambient token variables: a canary one is set for the run and must be absent, and `hook_env_deny` applies. | scenario `hooks_pass_through` | - |
| `hk.group_kill` | #33 | A hook that times out is vetoed and its process group, background processes included, is killed. | scenario `hooks_timeout_and_async` | - |
| `hk.async` | #33, #82 | An async `post-post` hook (a 20 second sleep) is not waited for and its working directory is swept after a day only when empty (OPEN DECISION: async hooks, #82; the marker is recorded, nothing asserted about it). | scenario `hooks_timeout_and_async` | Leave an async hook's working directory in the state `tmp/` directory, age it by a day (`touch -d`) and run any command: confirm it is removed only when empty. |
| `eo.forgectl_scope` | #34 | A write that forgectl escalates (exit 13) fails without the flag and succeeds with `--override-escalation=forgectl`; how forgectl audits it. | scenario `override_escalation` | Read forgectl's audit record of the overridden write (`forgectl export`) and confirm it names the escalation. |
| `ts.fence_255` | #19 | GitHub's and GitLab's own engines share the 255 fence-length limit: 254, 255, 256 and 300 backtick and tilde runs, and a suggestion with both kinds, are stored with the right fence and render as the same single code block as a short degraded control (the shape is also compared with an applicable control, recorded). | scenario `fence_limit` | - |
| `ts.run_split` | #19 | A degraded suggestion with backtick and tilde runs of 255 in one suggestion splits with the visible `<run split>` marker (and only then), and a backtick run of 255 or more gets a tilde fence of at most 255 characters. | scenario `fence_limit` | - |
| `ts.degraded_suggestion` | #19 | A degraded suggestion is posted inline as a text block and is not applicable. | scenario `fence_limit`<br>scenario `review_suggestions` | - |
| `ts.applicable_suggestion` | #22 | An applicable suggestion is stored with the host's own fence (`suggestion`, or GitLab's `suggestion:-N+0`). | scenario `review_suggestions` | Open the inline comment in the web UI and confirm the Apply suggestion button is there. |
| `ts.moved_finding` | #9 | A finding whose line is not in the diff moves to the review body. | scenario `review_suggestions` | - |
| `ts.suggestion_infostring` | #22 | Whether either host treats a `suggestion` info string after the first word as a suggestion (recorded, not asserted; OPEN DECISION: a suggestion swallowed by an open fence). | scenario `suggestion_probes` | - |
| `ts.suggestion_longer_fence` | #22 | Whether either host applies a suggestion with a fence longer than three backticks (decides whether the `fence` degrade can be dropped; recorded, not asserted). | scenario `suggestion_probes` | - |
| `ts.suggestion_variants` | #22 | Tilde and indented suggestion fences (recorded). | scenario `suggestion_probes` | - |
| `ts.mention_filters` | #22 | Neutral mentions, `a@user` and `1@user` do not link on either host; a literal mention does (the positive control). | scenario `mentions` | - |
| `ts.fullwidth_fold` | #22 | Whether either host folds fullwidth `＠`, `＜` or `！` to ASCII (recorded: what is stored, and the image and bold tags of the render; OPEN DECISION: fullwidth `＠`). | scenario `mentions` | - |
| `ts.entities_unterminated` | #22 | Whether either host decodes unterminated entities before the mention filter. | scenario `mentions` | - |
| `ts.gitlab_mentions` | #22 | GitLab: `@all` and group forms do not link. | scenario `mentions` | - |
| `ts.gitlab_engine` | #22 | GitLab's own Markdown (`>>>` quote, `[[_TOC_]]`, math, quick actions, references) behaves as pr-comment assumes; a slash line is not run as a quick action. | scenario `gitlab_markdown` | - |
| `ts.github_media` | #22 | A bare attachment URL on its own line renders as media on GitHub (recorded; `counts.images` does not count it). | scenario `render_probes` | - |
| `ts.github_diagrams` | #22 | Diagram fences (`mermaid`, `geojson`, `stl`) render on GitHub (recorded). | scenario `render_probes` | - |
| `ts.link_forms` | #22 | How a `javascript:` or relative link renders (recorded: the `javascript:` hrefs found). | scenario `render_probes` | - |
| `ts.postprocess` | #22 | Host post-processing after the engine: sanitizing, emoji, issue and commit links, `@` inside a link text (recorded). | scenario `render_probes` | - |
| `ts.marker_display` | #22 | How a host shows the hidden marker comment (not as text). | scenario `render_probes` | - |
| `ts.engine_version` | #22 | Whether GitHub's engine is the pinned `cmarkgfm` version at all. | - | Take the pinned fuzz corpus's disagreement cases (`tests/test_text_safety_fuzz.py`), post them as comments and compare the rendered HTML with `cmarkgfm`'s. |
| `ts.line_separators` | #22 | Whether hosts treat U+2028, U+2029, VT, FF and NEL as line breaks (cmark does not; D-21 and the goldens rely on that; recorded). | scenario `line_separators` | - |
| `ts.dedupe_line_format` | #20 | The dedupe line format `tests/textsafety.py` hard-codes, and how forgectl joins it to the body. | scenario `body_roundtrip` | - |
| `ts.stdin_trailing_newline` | #20 | A trailing newline on stdin does not change the stored text or `body_sha256`. | scenario `body_roundtrip` | - |
| `ts.unicode_whitespace_marker` | #20 | Whether forgectl treats Unicode whitespace (NBSP) as empty when it looks for the last non-empty line. | scenario `marker_whitespace` | - |
| `ts.host_keeps_bytes` | #20 | The hosts keep the marker comment bytes and trailing whitespace verbatim. | scenario `body_roundtrip` | - |
| `ts.neutralizing_equal` | #20 | forgectl's neutralizing leaves pr-comment's output unchanged (the `neutralizing_mismatch` warning path never fires). | scenario `body_roundtrip` | The warning path itself needs forgectl to change a body: post a body with a `forgectl:dedupe:` line in a code block through `forgectl comment` directly and compare. |
| `ex.closed_pipe` | #38 | A closed stdout pipe with a real pager is silent and exits 0. | test `tests/live/test_live_local.py::test_a_closed_pipe_with_head_ends_quietly_with_exit_zero`<br>test `tests/live/test_live_local.py::test_a_closed_pipe_with_less_on_a_terminal_ends_quietly` | - |
| `ex.unsafe_files` | #38 | The unsafe-file refusals (symlink, FIFO, directory, hard link, group-writable, foreign owner) on a real filesystem. | test `tests/live/test_live_local.py::test_unsafe_audit_files_are_refused_on_a_real_filesystem` | The foreign-owner case needs root (it is skipped otherwise): run it once with `sudo`, or `chown` a copy of `audit.jsonl` to another user in a scratch state directory and confirm exit 12 `audit_unavailable`. |

## Not run by the author

The host-facing scenarios were written and exercised offline only, in `plan` mode and against the pinned fake forgectl in `preflight` and `run` mode. No scenario has run against a real host, so every assertion about a host's answer is a prediction from the documents until the maintainer runs the suite once and corrects what it finds.
