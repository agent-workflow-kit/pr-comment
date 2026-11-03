# pr-comment: design

Status: designed, 2026-11-03 (interface 0.1.0-design). The normative rules are in [interface.md](interface.md); this document records the decisions, the reasoning and the internal structure, and links to interface headings instead of restating them.

## Role and layer

Layer 2 (composite). Posts reviews, replies and comments to PRs, safely and with attribution, only through forgectl's post primitives. Works fully standalone (architecture C2): a human can post a hand-written draft or a comment with nothing else of awkit installed except forgectl. It is shareable and runs on Linux, WSL and macOS (C4).

## Uses

| Tool | Binding | What for |
|---|---|---|
| forgectl | A (hard) | Every host read and write: `show`, `diff --json`, `files`, `compare`, `comments`, `whoami`, `review`, `comment`, `reply`, `approve`, `resolve`, `unresolve`, and each post command's `--dry-run` as the [pre-check](interface.md#pre-check) ([forgectl edge](interface.md#forgectl-edge)). |

pr-comment never calls `gh` or `glab` and holds no credentials. Consumers: review-run (the [review draft](interface.md#review-draft) schema, optionally `draft new`), review-watch (`review`, `--reanchor-to`, `approve`), review-respond (`reply`).

## Decisions

| # | Decision | Where |
|---|---|---|
| D-1 | Stateless: no database, only audit, log and a temporary directory that holds only hook working directories, checked and swept by file descriptor on every run. The draft carries its anchor and `draft_id`; "already posted" is forgectl's answer on the real call. | [Files](interface.md#files) |
| D-2 | The review draft is strict JSON with an integer `schema_version`; extra data only under `meta`. A draft carries the findings it could not anchor in `unanchored`, so storing a re-anchored draft loses nothing. | [Review draft](interface.md#review-draft) |
| D-3 | Re-anchoring is exact hunk mapping through forgectl `compare`, with no fuzzy matching; anything unmappable moves to the body with a reason. File sections are read only from `---`/`+++` and rename lines; anything ambiguous moves (`diff_unavailable`). | [Re-anchoring](interface.md#re-anchoring) |
| D-4 | Left-side findings keep their lines only while the merge-base is unchanged (`anchor.merge_base_sha`), else move (`base_changed`); checked on every review that has left-side findings, not only on re-anchor. | [Re-anchoring](interface.md#re-anchoring) |
| D-5 | Two attribution templates chosen by the actor, put at the top of the body; the marker is appended in code and cannot be configured away; every inline comment carries the bare marker on both hosts; detection is normative. | [Footers and marker](interface.md#footers-and-marker), [Marker detection](interface.md#marker-detection) |
| D-6 | Mentions are neutralized by replacing `@` and its entity forms with `＠` in all untrusted text, except pr-comment's own applicable-suggestion lines; an allowlist and a human-only, interactive per-run override. | [Mentions](interface.md#mentions) |
| D-7 | Command surface: `review`, `comment`, `reply`, `approve`, `resolve`, `unresolve`, `draft check`, `draft new` (D-23), `export`; no TUI. `resolve` reads the PR, thread state and the account first, with one dialog for all threads (at most 100). | [Commands](interface.md#commands) |
| D-8 | Optional `--expected-head` on `comment` and `reply`; no head check on `resolve`. | [Comment and reply order](interface.md#comment-and-reply-order) |
| D-9 | pr-comment confirms the final plan itself, which is the posted text: it applies forgectl's documented neutralizing before showing it, and then calls forgectl with `--yes`. | [Confirmation](interface.md#confirmation), [Human output](interface.md#human-output) |
| D-10 | One hook pair, `pre-post` and `post-post`, with an `action` field; bodies only on a per-hook `with_text` opt-in. Hooks run in a private empty directory without token variables (design guide section 8, architecture 13.3). `--override-escalation=<scope>` lets a caller's `confirm` override only the tool that escalated (architecture 12.4). | [Hooks](interface.md#hooks) |
| D-11 | Deterministic dedupe keys from the inputs: `prc:rv:<draft_id>` for `comment` and `request_changes` reviews, `prc:ra:<draft_id>:<12 hex of the pinned head>` for `review --approve`, and content keys hashed over `[pr_id, "comment", body]`, `[pr_id, "reply", to, body]` and `[pr_id, "approve", head, base, body]`, with the body as given (before mentions, attribution line and marker); no automatic write retries. | [Dedupe keys](interface.md#dedupe-keys) |
| D-12 | Pre-check with forgectl's own dry run before the dialog; invalid positions and cut file lists move findings before confirmation; nothing changes after it. The pre-check never ends a command: forgectl's real post decides whether anything is left to do. | [Pre-check](interface.md#pre-check), [Position validation](interface.md#position-validation) |
| D-13 | Suggestions are a structured field rendered by pr-comment, always with a three-backtick fence, and degraded to plain code when they cannot post safely (a `/` line, a run of three or more backticks or tildes, invisible characters); untrusted text may not carry its own suggestion fence. A moved finding's suggestion is shown as code with the moved note. | [Suggestions](interface.md#suggestions) |
| D-14 | GitLab: the body thread of a non-blocking (comment or approving) review is resolved automatically (configurable), also on a retry that finds the review already posted. | [GitLab review body thread](interface.md#gitlab-review-body-thread) |
| D-15 | Normative orders for every posting command; a retarget needs a new draft; no automatic re-anchor. | [Review order](interface.md#review-order) |
| D-16 | One audit record pair per command, `attempted` before the first forgectl write and after its hooks, each write listed in the outcome; refusing to post when the audit file is not writable. | [Audit and log](interface.md#audit-and-log) |
| D-17 | No credentials, no repo from the working directory, `tool_missing` and `lower_contract_mismatch` for the forgectl edge. The PR is always read first; forgectl's canonical form drives keys and config lookups. Forwarded values are validated and passed in `--flag=value` form. `forgectl_bin` resolves only against absolute `PATH` entries; additive forgectl changes are not a contract mismatch. | [forgectl edge](interface.md#forgectl-edge) |
| D-18 | Automatic approvals need `allow_auto_approve`, a decision id and a draft that was not re-anchored (the gate saw another head) and the caller's `--expected-head` (the head its gate decided on), in addition to the caller's gate; `--approve` on a `request_changes` draft is refused for every actor. | [Automatic approval guard](interface.md#automatic-approval-guard) |
| D-19 | Exit codes stay in the reserved range; forgectl's failures pass through with its envelope in `lower_error`; exit-12 envelopes say whether anything landed and how to retry. | [Exit codes](interface.md#exit-codes) |
| D-20 | Every approval posts a body (at least the marker), so "already reviewed by me" detection always works. | [Approve order](interface.md#approve-order) |
| D-21 | One text pipeline; pr-comment pre-applies forgectl's neutralizing, so the text it parses, checks, shows, hashes and confirms is the text forgectl posts. | [Text pipeline](interface.md#text-pipeline) |
| D-22 | Untrusted Markdown is neutralized by character, not parsed: `<` and `!` before `[` become fullwidth, so no raw HTML or image can be posted in any engine; the attribution line comes first, so no untrusted construct can hide it. | [Body structure](interface.md#body-structure) |
| D-23 | `draft new` (also on a PR whose diff is too large) prints a valid skeleton draft (fresh `draft_id`, canonical `pr`, consistent anchor) and writes nothing. | [draft new](interface.md#draft-new) |
| D-24 | `--expect-plan` binds a preview to the post, checked before a dry run prints, so a caller's preview of a preview is bound too. | [Posting plan](interface.md#posting-plan), [Global flags](interface.md#global-flags) |

### Reasoning for the less obvious decisions

- **D-1.** A database would duplicate forgectl's dedupe and review-watch's draft lifecycle, and losing it could cause double posts. Everything a retry needs is derivable from the draft and the PR. "Already posted" is forgectl's answer on the real call, which is idempotent by key; rerunning the same command (with `--reanchor-to` after a push) is the check, and a dry run gives a hint only. Review pass 2 found that forgectl's dry run does not promise its dedupe answer when a state step is still needed, so no pr-comment control flow depends on it. The temporary directory holds only hook working directories, because forgectl reads post input on stdin.
- **D-2.** Markdown with front matter breaks when untrusted text contains fences or `---`; YAML has implicit typing. JSON keeps untrusted text inside string fields. Editing is review-watch's concern.
- **D-3.** Fuzzy matching recovers more findings but can put a comment on the wrong line, which is worse than moving it to the body. Spike A showed the exact mapping is independent of the diff's context size. The `diff --git` line cannot be split reliably when paths contain spaces or a space followed by `b/`, so paths come only from the `---`, `+++` and rename lines.
- **D-4.** Review pass 2: the base tip can move under an unchanged head, so the left-side check runs on every review that has left-side findings. Spike A showed that mapping left-side lines through the head-to-head diff after a base merge lands on valid but wrong lines without any error. Mapping through the old and new merge-base would need a second comparison that forgectl's PR-scoped `compare` may not serve.
- **D-5.** The attribution line is first so that no untrusted construct can hide it (D-22); the marker stays the last line, which marker detection and forgectl's dedupe read as raw text. Marking every inline comment on both hosts lets consumers recognize pr-comment's content with one rule and one lookup, instead of a second lookup per comment through its review.
- **D-6.** Spike B showed entities and `\@` link and a mis-sized fence leaks. Review pass 1 then showed that inserting code spans can create live mentions (stray backticks, backslashes) and that matching needs decoded text. Replacing one character changes no structure and covers every decoded form. Review pass 2 then showed that markdown-it-py's code-block ranges differ from GitHub's engine (cmark-gfm) on ordinary inputs (a link reference definition followed by an indented line, a table followed by a row-less `|` line, a tab-indented `>` continuation): a differential fuzz found 406 distinct leaking inputs in about 360,000 cases, and about one in 600,000 still leaked with the exemption reduced to fences. An exemption by parsed ranges therefore leaks, while replacing everywhere depends on no engine. The cost is that `@` in inline code, code blocks (decorators, annotations), URLs and prose shows as `＠`; only pr-comment's own applicable-suggestion lines are unchanged, and they are code by construction because no suggestion line can close a fence.
- **D-7.** `resolve` reads the PR, the thread state and the account before the dialog, and skips threads already in the requested state before the dialog, hooks and audit, as forgectl does, and shows what each thread is.
- **D-8.** review-respond pushes its fix and then replies, so a mandatory head check would refuse its own replies.
- **D-9.** If forgectl prompted, the user would confirm text pr-comment had already changed (attribution lines, moved findings, mentions). The dialog shows untrusted text display-sanitized, so escape sequences cannot rewrite what the human approves (review pass 1).
- **D-11.** A changed `event` or an approval after a posted review are new actions with their own keys or commands (review pass 2). Keys come from the inputs, not the rendered text, so retries reuse them under a new decision id, config or tool version. The review key omits the anchor so that re-anchoring a posted draft cannot post it twice; a new review is a new draft. The approve keys include the head so a re-approval after a push is a new action (review pass 1).
- **D-12.** The pre-check shows the plan, but the real call always runs: forgectl's post is idempotent by key (it posts nothing again, completes a pending state step, or exits 12), so a short-circuit on a dry run's answer would swallow a pending state step (review pass 2). Review pass 1 found the post-confirmation fallback posted text no hook or human saw, and that local validation duplicated forgectl's rules with read races. Running forgectl's own checks once, before the dialog, costs one extra forgectl invocation per post.
- **D-13.** forgectl neutralizes every line starting with `/`, including in suggestion blocks, so such a suggestion would apply wrong code. Longer suggestion fences are unverified on both hosts. A suggestion line that closes a fence left open by the body would end the code block early, and a suggestion fence written into a body would bypass every degrade (review pass 2). An applicable suggestion is one click from a commit by the PR author, so invisible or bidirectional characters degrade it too.
- **D-14.** On GitLab projects that require resolved threads, every non-blocking review would otherwise block merges. A `request_changes` review stays blocking unless config says `always`.
- **D-16.** Each write carries its start time for pairing with forgectl's records. One pair per command, as for forgectl's writes, gives one id per run; refusals, vetoes and escalations before the first write go to the log.
- **D-18.** An automatic approval is refused when the draft was re-anchored, since the gate saw another head, and needs the caller's `--expected-head`, since a draft's own `anchor.head_sha` is editable and cannot prove what the gate saw. Approval is the highest-impact action pr-comment takes; architecture C7 asks for safe defaults at each tool, not only in callers. An approval of a draft that requests changes is a contradiction, not a gate decision.
- **D-21.** A parse of different text than the host receives leaks a mention and splits suggestions: a line separator such as LS, VT or NEL is a line break to forgectl but not to a Markdown parser (review pass 1). forgectl's steps are idempotent, so pre-applying them leaves its own pass a no-op apart from its dedupe line.
- **D-22.** Raw HTML, an open fence or a `<details>` in untrusted text can hide the mandatory automatic attribution, and raw HTML and images can hide text or fetch a URL when the page is viewed, under the user's name (review passes 1 and 2). Review pass 2 showed that markdown-it-py and GitHub's engine disagree on block structure, so token-based refusals and a structure check verified with one parser do not hold on the host (an unclosed `<details>` hid the automatic footer; a definition in one field made an image in another). Every raw HTML construct needs a literal `<` and every image form a literal `![`, so replacing those characters covers every engine with no parser. Keeping a footer outside every open block at the end of a body needs a parser; putting the attribution line first needs none. The cost: `<` in code shows as `＜`, `![` as `！[`, and an unclosed fence can pull the marker and later sections into a code block (visible, harmless).
- **D-23.** A standalone human (C2) needs valid ids and a consistent anchor without building a ULID or reading the diff and the PR by hand; reading both and checking their heads removes the anchor race. It works on a PR whose diff is too large (merge-base unknown). review-run may use it too.
- **D-24.** Callers such as review-watch confirm in their own dialogs; passing the `plan_sha256` of the `--dry-run` plan they showed with `--yes` makes pr-comment refuse if anything changed since.

## Resolved open topics

The three open topics of architecture section 20 are resolved: the review-draft format (D-2, D-13, [Review draft](interface.md#review-draft)), the re-anchoring algorithm (D-3, D-4, [Re-anchoring](interface.md#re-anchoring)) and the attribution templates (D-5, [Footers and marker](interface.md#footers-and-marker)).

## Live verification

| Spike | What | Result |
|---|---|---|
| A (local git 2.43) | Unified-diff edge cases for the hunk mapping: renames, `\ No newline`, CRLF, binary and mode changes, deletions, hunk header arithmetic, context sizes, base merges, force pushes, left-side lines, position validity. | 22 hand cases with 52 findings pass at every context size; 300 random edit pairs map without failure. Context lines map; only removed lines are unmappable; renames must carry the path; left-side mapping through the head diff is wrong after a base merge (D-4); a mapped line can fall outside the new PR diff (covered by the pre-check). |
| B (GitHub markdown render endpoint; nothing posted) | How mentions, code spans, fences and entities render. | Code spans suppress mentions in every context; entity forms and `\@` still link; the preceding character decides whether `@` links; a fence that closes early leaks a mention; the fullwidth `＠` does not link (D-6). GitLab's render endpoint needs a token and was not tested. |
| Review pass 2 (cmark-gfm, GitHub's engine, run locally; nothing posted) | Differential fuzz of the mention exemption and of the HTML, image and footer checks against a CommonMark parser; the tilde and suggestion-fence cases. | markdown-it-py and cmark-gfm disagree about code blocks, raw HTML and images in ordinary inputs, so parsed exemptions and structure checks leak (D-6, D-22). Character replacement never failed. A suggestion line `~~~` can close a tilde fence a body left open, and a suggestion fence in a body renders as an applicable suggestion (D-13). |

Still to verify, with conservative defaults in place:

- GitLab: `＠` does not link; `＜` and `！` are not syntax; `@all` and group forms.
- An applicable suggestion with a fence longer than three backticks on each host; if both apply it, the `fence` degrade can be dropped in a later minor release.
- Whether host diffs keep CR and `\ No newline` markers (mapping is unaffected either way).
- A GitHub suggestion with a degraded line (degraded by default).
- GitHub and GitLab: a bare attachment URL on its own line rendering as media, diagram fences (`mermaid`, `geojson`, `stl`), `1@user` and `a@user` not linking, and no folding of fullwidth `＠`, `＜` and `！` to ASCII.

Each becomes a `live` test in the plan.

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, global flags, actor resolution, interactive detection |
| `config.py` | Pydantic config, drop-ins, environment, per-repo merge, footer placeholder validation |
| `draft.py` | draft loading (no-follow, caps), schema, version and consistency checks, `draft check`, `draft new` |
| `diffmap.py` | unified-diff parser and hunk mapping for re-anchoring |
| `render.py` | text pipeline, character pass (mentions, `<`, images), finding text, suggestions, layout, attribution line and marker, dedupe keys |
| `plan.py` | the posting plan shown by the dialog and `--dry-run`, and its `plan_sha256` |
| `forge.py` | the forgectl edge: argv in `=` form, stdin input, the pre-check, result and envelope parsing by the fields pr-comment uses |
| `gateway.py` | the only subprocess boundary |
| `hooks.py` | `pre-post` and `post-post` |
| `audit.py` | audit and log records, `export` |
| `confirm.py` | the TTY dialog, display sanitizing and the summary |

## Data model

No persistent state. The review draft ([schemas/review-draft.json](../schemas/review-draft.json)) is the only document pr-comment reads besides config; the plan is an in-memory object rendered for the dialog, `--dry-run` and hooks; forgectl input is sent on stdin.

## Concurrency

No locks. Two simultaneous runs of the same post can both land; forgectl's dedupe lookup and the deterministic keys make sequential retries safe ([Residual risks](interface.md#residual-risks)).

## Error handling

Every failure before the first write sends nothing. forgectl's errors pass through with its exit code and its envelope in `lower_error` ([Error envelope](interface.md#error-envelope)). A post that landed but whose follow-up failed exits 0 with a warning (body-thread resolve) or exits 12 `outcome_partial` (GitLab approval after its note); every exit-12 envelope says whether anything landed (`landed`) and whether rerunning the same command is safe (`retry`).

## Dependencies

- `pyyaml` and `pydantic` (design guide section 3); `pydantic` also models the forgectl output fields pr-comment reads.
- No Markdown parser: mentions, `<` and images are neutralized by character (D-6, D-22), so `markdown-it-py` is not a dependency.
- `jsonschema` and `cmarkgfm` (tests only; `cmarkgfm` is GitHub's engine, pinned, used as the oracle for the text-safety tests).

## Test strategy

- All subprocess calls go through `gateway.py`; a fake `forgectl` in `tests/fakebin/` replays scenarios. forgectl schemas copied into `tests/`, pinned to a forgectl version, validate the fake's output; at runtime pr-comment checks only the fields it uses.
- Golden files for every `--json` output, every rendered body and the plan.
- Text pipeline (D-21): golden tests for the LS, VT and NEL cases of spike B and review pass 1, and a check that forgectl's fake reports 0 neutralized lines for pr-comment's output.
- Mentions (D-6): spike B's case files plus `\@x`, ``a`@x``, `@&#111;ctocat`, `&#64victim`, `é@x`, `@.x`, `@john.doe` with `john` allowed, and `--actor auto:@all` in the attribution line. Golden cases for the three divergence classes of review pass 2 (`[a]: /u` then an indented `@victim`; a table, a row-less `|` line, then an indented `@victim`; `> x` then a tab-indented `>` line with a fence and `@victim`) and the tilde case (an open `~~~` fence plus a suggestion line `~~~`), each rendered with the pinned `cmarkgfm` and checked for no `@name` in a text node outside `pre`, `code` and `a`; plus a differential fuzz over a token alphabet (fences, quotes, lists, tabs, tables, definitions) with the same oracle.
- Body structure (D-22): golden bodies for an open fence, `<!--`, `<details>`, `![](...)`, `![t][r]` with `[r]:` defined in another finding, `[r]: /u` then an indented `<details>` and `<img src=...>`, and an open `mermaid` fence, each rendered with the pinned `cmarkgfm` and checked for no `raw HTML omitted`, no `<img` and the attribution line as the first paragraph; a path with backticks and `[x](u)`, a path with U+202E and a zero-width space (markers in the span), a title with `**` and `\n`; and a check that an inline comment's suggestion stays applicable after a closed body.
- Suggestions (D-13): golden cases for a suggestion fence written into a body (plain and entity-encoded info string, inside a quote) and for a suggestion line `~~~`.
- Text pipeline inputs: `"x\n"` as a `title` and as a suggestion line, and a draft with `"\ud800"` (exit 2, `control_character`).
- Pre-check (D-12): the fake forgectl returns `invalid_position` and `too_large` from `--dry-run`; a test that the bytes on the real call's stdin equal the confirmed plan; the fake forgectl's dry run reports `already_posted: true` with a pending GitLab approval and the real call completes it; `draft new` with the fake `diff` exiting 20 `too_large`.
- `diffmap.py` table tests from spike A's cases (real `git diff` output at several context sizes), plus paths with spaces, a space followed by `b/`, quotes, newlines (as forgectl marks them) and non-ASCII, fixtures in forgectl's GitLab-assembled form once forgectl specifies it, and a property test against a reference mapper.
- `live` tests (deselected by default, never in CI), once forgectl is implemented: a review with inline comments, a re-anchor after a push, a GitLab body-thread resolve, mention neutralizing on both hosts, and the items under "Still to verify".
