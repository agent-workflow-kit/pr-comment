# pr-comment: implementation plan

Status: planned, 2026-11-03. Implements [interface.md](interface.md) (normative) using the internal design in [design.md](design.md). Work is tracked as GitHub issues in this repository; each issue names its stage below. When an issue is picked up, its implementer expands it into bite-sized test-first steps. This plan holds the stages and order, not code.

## Conventions for every stage

- Test-first. Every behavior starts as a failing test.
- The only process pr-comment starts for its work is forgectl, and it goes through `src/pr_comment/gateway.py`, which owns argv, stdin, the environment, the working directory and timeouts. Hooks use their own module with the exact-PID and process-group rules. Unit tests replace the gateway with an in-process fake.
- Process tests run the real CLI against a fake `forgectl` in `tests/fakebin/`, first on `PATH`, which logs argv, stdin and environment and replays scripted results. The fake validates every result it returns against forgectl's schemas, copied into `tests/` and pinned to a forgectl version. These tests assert the edge rules on every command: values passed as `--flag=value`, bodies and review input only on stdin, `--yes` only after pr-comment's own confirmation, the pinned `--expected-head` and `--expected-base`, and the real call's input equal byte for byte to the confirmed plan.
- Every `--json` output gets a golden-file test validated against `schemas/`; every hook payload, log record and audit record written in a test is validated too. `tests/golden/` holds no real names.
- Rendering is tested against GitHub's own Markdown engine: `cmarkgfm`, pinned, test-only, renders every golden body, and the tests assert that no mention links, no raw HTML or image survives and no applicable suggestion exists that pr-comment did not write. A differential fuzz test runs the same checks over generated inputs.
- `live` tests (marker, deselected by default, never in CI) post to throwaway repositories and re-check each "still to verify" row of design.md's Live verification. A failure means a host changed.
- Every behavior design.md lists as relied on becomes a regression test in the stage that relies on it; spike A's cases and the review passes' cases become fixtures in the stage that owns them.
- `make verify` passes before every commit. PR titles are Conventional Commit headers; no `!` before 1.0.
- Runtime dependencies: `pyyaml`, `pydantic`. Test-only: `jsonschema`, `referencing`, `cmarkgfm`. No Markdown parser at runtime and no HTTP library.

## Module map

| Module | Stage | Responsibility |
|---|---|---|
| `cli.py` | 0 | argparse surface (the full, frozen command and flag set), global flags, actor and interactive detection, human-only and interactive-only flag rules, flag value validation, dispatch |
| `output.py`, `errors.py` | 0 | result objects, error envelope with `landed` and `retry`, exit codes |
| `config.py` | 0 | YAML and `config.d` merge, Pydantic models, defaults equal to `config.example.yaml`, `PR_COMMENT_*` overrides, `repos.<id>` lookup, footer template validation, config hash |
| `gateway.py` | 0 | the forgectl process boundary: argv, stdin, environment, `PATH` resolution of `forgectl_bin`, signal forwarding, deadline |
| `files.py` | 0 | state and temporary directory checks, no-follow and exclusive creation, sweeping |
| `audit.py`, `jsonlog.py` | 0 | `audit.jsonl` record pairs with `writes`, `log.jsonl`, `export` |
| `display.py` | 0 | Human output: display sanitizing with forgectl's rules |
| `draft.py` | 1 | draft loading, schema, version, caps and consistency checks, `draft check` |
| `render.py` | 1 | Text pipeline: input checks, character neutralizing, mentions, suggestions and their degrading, finding text, body assembly, attribution line and marker, pre-applied forgectl neutralizing |
| `keys.py` | 1 | dedupe keys and `plan_sha256` |
| `diffmap.py` | 2 | unified-diff parser and hunk mapping, left-side merge-base rule, `unanchored` |
| `forge.py` | 3 | forgectl calls, field checks of its output, `lower_error`, `lower_contract_mismatch` |
| `plan.py` | 3 | the posting plan, Pre-check rounds |
| `confirm.py` | 3 | the TTY dialog and its summary |
| `post.py` | 3 | Review, Approve, and Comment and reply orders; automatic approval guard; `--expect-plan` |
| `hooks.py` | 4 | hook processes, payloads, outcomes, decision ids, override scopes |
| `resolve.py` | 4 | Resolve order |
| `gitlab_thread.py` | 4 | GitLab review body thread follow-up |

## Stage 0: Foundations

**Goal**: A CLI that accepts the whole frozen surface, reads config, and can run forgectl and write audit and log records safely.

**Success Criteria**: `pr-comment --version` works; every command parses and returns the error envelope; human-only and interactive-only flags are refused otherwise; flag values that forgectl would read as options are refused (exit 2); invalid config exits 2 naming the key, including unknown footer placeholders and an empty `footer.auto`; the gateway resolves `forgectl_bin` only against absolute `PATH` entries and exits 12 `tool_missing` with the install command; an unwritable audit file or an unsafe temporary directory exits 12 before any forgectl call; `audit.jsonl` and `log.jsonl` validate against their schemas; display sanitizing marks every class forgectl marks.

**Tests**: config precedence (flags, `repos.<id>` matched case-insensitively, environment, file, defaults) and drop-in merge rules; actor resolution and `actor_source`; flag value tables (leading `-`, sha and ref grammars, decision-id and dedupe-key grammars); gateway process tests with the fake forgectl (argv form, stdin, no shell, exact-PID signal forwarding and exit 128+N before any write); temporary-directory tests for symlinks, wrong owner or mode, and sweeping only `prc-` entries; display sanitizing table over every code point class.

**Status**: Not Started

## Stage 1: Drafts and rendering (0.1)

**Goal**: Drafts load and validate offline, and every body is rendered exactly as the Text pipeline says.

**Success Criteria**: `draft check` passes and fails exactly as the Review draft section says, with pointers; every rendered body has its attribution line first and the marker last; mentions, `<` and `!` before `[` are neutralized in all untrusted text except pr-comment's own applicable-suggestion lines; suggestions degrade for every listed reason; suggestion fences in untrusted text are refused; dedupe keys match the interface's test vector; pre-applying forgectl's neutralizing is idempotent.

**Tests**: golden bodies for every rule in Text pipeline, Body structure, Finding text, Moved findings, Suggestions, Mentions and Footers and marker; the spike B and review-pass cases (entity forms, backslash, stray backticks, line separators, fence and tilde breakouts, reference-style images split across findings, an indented tag after a reference definition, suggestion fences written with entities) rendered with `cmarkgfm` to assert no live mention, HTML, image or foreign suggestion; the differential fuzz test; one-line field and surrogate checks; Marker detection table (a body ending in a quoted marker, a forgectl dedupe line inside untrusted text, trailing whitespace).

**Status**: Not Started

## Stage 2: Re-anchoring (0.2)

**Goal**: `--reanchor-to` maps findings exactly or moves them with a reason, and keeps every finding in `reanchored_draft`.

**Success Criteria**: every spike A case maps as recorded at every context size; renames are followed; deleted, binary and unreadable sections move with their reasons; `diff: null` and `too_large` move every right-side finding; left-side findings keep their lines only when both merge-bases are known and equal; `unanchored` carries every finding that did not map.

**Tests**: spike A's cases and fuzz pairs as fixtures; path parsing from `---`/`+++` and rename lines with quoted, spaced, marked and non-ASCII paths; a base merge between heads; a force push (`diverged`); a re-anchor of a draft that already has `unanchored` entries.

**Status**: Not Started

## Stage 3: Posting (0.3)

**Goal**: `review`, `comment`, `reply` and `approve` post through forgectl following their orders, with one confirmation over the exact posted text.

**Success Criteria**: every order runs its steps in sequence; the pre-check runs forgectl's own dry run before the dialog, moves findings on `invalid_position` and incomplete file lists, and never ends a command; the dialog shows the display-sanitized plan and summary; the real call's stdin equals the confirmed plan; `--expect-plan` refuses a changed plan; the automatic approval guard and `auto_approve_reanchored` refuse as specified; `stale_head`, `stale_base` and `not_open` refuse before rendering; forgectl's errors pass through with `lower_error`, `landed` and `retry` per the interface's table; audit records pair `attempted` and outcome with `writes`.

**Tests**: process tests with the fake forgectl for each order and each exit path (veto, escalation, declined dialog, non-interactive without `--yes`, `outcome_unknown`, GitHub and GitLab `outcome_partial`, `quick_action_ran`, `approved_stale_head`, `drafts_present`, `pending_review_exists`, `invalid_position` on the real call); a fake dry run that reports `already_posted: true` with a pending GitLab approval, which must still make the real call; dedupe-key retries after `outcome_unknown` at the same head and after a push with `--reanchor-to`; `review --approve` key separation; golden results for every command, dry runs included.

**Status**: Not Started

## Stage 4: Hooks, resolve and follow-ups (0.4)

**Goal**: `pre-post` and `post-post` hooks, `resolve` and `unresolve`, the GitLab body-thread follow-up, scoped `--override-escalation`, `draft new` and `export`.

**Success Criteria**: hooks run per forgectl write with the documented payloads, environment and working directory, vetoes and escalations behave as in the design guide, and hook decision ids are recorded and forwarded; `--override-escalation=pr-comment` and `=forgectl` override only that tool's escalations; `resolve` reads `show` and `comments`, skips threads already in the requested state, shows one dialog and reports per-thread results; the GitLab body thread of a non-blocking review is resolved after a fresh post and after a rerun that finds it posted, with the `body_thread_unresolved` warning on failure; `draft new` builds a valid draft, also when `diff` is `too_large`; `export` prints both files.

**Tests**: hook process tests (timeouts signal the hook's own process group, `with_text` bodies, token variables removed); escalation scope tests against the error envelope's `escalated_by`; resolve with an incomplete `comments` read and a failing `whoami`; GitLab thread identification by the dedupe line; `draft new --from` with a stale anchor.

**Status**: Not Started

## Stage 5: Live suite and macOS (0.5)

**Goal**: Re-check every relied-on host behavior and every "still to verify" row on real hosts, and run the whole suite on macOS.

**Success Criteria**: on a throwaway GitHub repository and, with a token, a throwaway GitLab project: a review with inline comments and an applicable suggestion, a degraded suggestion, a re-anchor after a push, the GitLab body-thread resolve, an approval with its body, and replies of every id kind all post and are found by Marker detection; `＠`, `＜` and `！` render inert on both hosts, including `@all` and group forms on GitLab; the full non-live suite passes on macOS.

**Tests**: the `live` suite; a macOS run of `make verify` recorded in the release notes of the version that declares macOS support.

**Status**: Not Started

## Version roadmap

- **0.x:** stages 0 to 5 in order, one minor release per stage (0.1 drafts and rendering, 0.2 re-anchoring, 0.3 posting, 0.4 hooks and follow-ups, 0.5 live suite and macOS). Interface changes before 1.0 are `feat` commits whose titles name the break, with the interface registry updated.
- **1.0 qualifies when:** every stage is done; the `live` suite passes on both hosts against a released forgectl; the GitLab rendering rows of Live verification are settled; review-watch and review-respond have used the CLI and its `--json` output without a needed change for one minor release; the forgectl carry-forwards pr-comment relies on (audit `dedupe_key`, `attempt_id` in results) have landed or been replaced by a documented pairing rule.
- **1.x (additive):** dropping the `fence` degrade reason once longer suggestion fences are verified on both hosts; a finding-to-comment id map in results if a consumer needs one; new draft fields that are optional.
- **2.0:** nothing planned. A change to the marker, the dedupe key forms or the draft's required fields would be one.
