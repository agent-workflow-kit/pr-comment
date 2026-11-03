# pr-comment: interface

Status: 0.1.0-design, 2026-11-03. Written before implementation (architecture section 18.1). This document is the single normative source for pr-comment's rules; `docs/design.md`, the README and the architecture link to its headings and do not restate them.

pr-comment posts reviews, replies and comments to pull and merge requests, safely and with attribution. It posts only through forgectl's post primitives and never runs `gh` or `glab` itself. It is stateless: it keeps no database, only `audit.jsonl`, `log.jsonl` and a temporary directory.

References: architecture (`../awkit-docs/architecture.md`) sections 5.2, 12.4, 13.2 and 14; design guide (`../awkit-docs/design-guide.md`); forgectl's interface (`../forgectl/docs/interface.md`), cited below as "forgectl, section X".

## Concepts

### Identifiers

- A PR is given as a URL or `host/<full project path>#N` (GitLab MR numbers without `!`). pr-comment reads it with `forgectl show` before any other host step and uses forgectl's canonical `pr` (the `#N` form) and `pr_id` everywhere: in dedupe keys, hooks, audit records, the attribution line's `{sha}` and `repos.<id>` lookups.
- pr-comment never reimplements forgectl's reference grammar. It only rejects an argument that is neither `https://...` nor `<something>#<digits>`, or that starts with `-` (exit 2 `invalid_pr`), and lets forgectl parse the rest.
- The repo id is `host/<full project path>`, taken from `show`'s `repo`. pr-comment never derives a repo from the working directory.
- Comment, review and thread ids are forgectl's (forgectl, section Identifiers): `gh:ic:`, `gh:rc:`, `gh:rt:`, `gh:r:`, `gl:d:`, `gl:n:`. pr-comment treats them as opaque.
- `draft_id` is 26 characters of Crockford base32 (`^[0-9A-HJKMNP-TV-Z]{26}$`), chosen once by whoever creates a draft; [`draft new`](#draft-new) writes a ULID.
- A finding `id` is `^[A-Za-z0-9._-]{1,64}$`, unique across a draft's `findings` and `unanchored`.

### Actors and interactive runs

- The actor comes from `--actor` (grammar `^(human|auto):[A-Za-z0-9._@-]{1,64}$`); the default is `human:<me.name>` when stdin is a TTY, else `auto:unknown`.
- `me.name` must match `^[A-Za-z0-9._@-]{1,64}$`; an OS username that does not gives `human:unknown`.
- A run is **interactive** when stdin and stderr are TTYs, the actor is not `auto:`, `--yes` is not given and no body or draft is read from stdin (`-`).
- `--body-file -` or `--draft-file -` with stdin a TTY exits 2 (`usage`).
- Human-only flags (`--allow-mention`, `--override-escalation=<scope>`) require a `human:` actor; otherwise exit 2. `--allow-mention` also requires an interactive run (exit 2 otherwise); its names show in the dialog summary and the audit record (`inputs.allowed_mentions`, `override: ["allow_mention"]`).
- A human who pipes a draft (`--draft-file -`) gets the default actor `auto:unknown` (stdin is not a TTY) and the automatic attribution; such a pipeline passes `--actor human:<me>` and `--yes`, or the human writes the draft to a file and runs `review --draft-file <path>`.
- As in forgectl, human-only rules are advisory against a same-user process that claims `--actor human:<user>`; the audit record shows how the actor was obtained (`actor_source`, `stdin_tty`).

### Posting plan

Every posting command first builds a **plan**: the target PR, the pinned head and base, the event, the exact bodies that will be handed to forgectl (the output of the [Text pipeline](#text-pipeline), plus forgectl's dedupe line), each inline comment with its path and line, the fate of every finding and the follow-up writes. The plan is what the confirmation dialog shows, what `--dry-run` prints and what `pre-post` hooks describe. It is checked by forgectl's own dry run before it is shown ([Pre-check](#pre-check)). pr-comment never posts text that is not in the confirmed plan.

- `plan_sha256` is the sha256 of the canonical JSON (keys sorted, `ensure_ascii=False`, separators `,` and `:`, UTF-8) of `{pr, action, event, head_sha, base_ref, dedupe_key, bodies: [{role, text}], inline_comments: [{finding_id, path, line, start_line, side, role}], follow_ups}`. `text` is each body as handed to forgectl, without forgectl's dedupe line (the key enters as `dedupe_key`); `event` is null for `comment` and `reply`; `follow_ups` is as in the plan output ([Post result](#post-result)). `bodies` is `body` first, then `inline:<finding id>` in draft order; `inline_comments` are in draft order; `follow_ups` in write order.
- The hash covers the bodies, so it changes with anything that enters them: the actor (template choice, `{user}`, `{policy}`), the first `--decision-id` when a template uses `{decision_id}`, the config (`footer`, `mentions`, `gitlab`), `--allow-mention`, the pinned head and base, and for `comment` and `reply` the head `show` reads when no `--expected-head` is given (it enters `{sha}` and `head_sha`). A caller previews with the same flags and config it will post with, and previews `comment` and `reply` after its push.
- `--expect-plan <sha256>` binds a preview to a post ([Global flags](#global-flags)).

## Review draft

The review draft is a JSON document described by [schemas/review-draft.json](../schemas/review-draft.json). review-run produces drafts (its artifact is a review draft with review-run's own data under `meta`); review-watch keeps the editable copy; `pr-comment review` posts one; [`draft new`](#draft-new) prints a skeleton. Every text field is untrusted, agent-generated text.

### Draft fields

| Field | Type | Required | Rule |
|---|---|---|---|
| `schema_version` | integer | yes | `1`. See [Draft versioning](#draft-versioning). |
| `draft_id` | string | yes | See [Identifiers](#identifiers). Kept when a draft is edited or re-anchored. |
| `pr` | string | yes | The PR in `host/<full project path>#N` form. Compared with `show`'s `pr`, host and path in ASCII lowercase. If they differ, pr-comment runs `forgectl show <draft pr>`; the same `pr_id` (a renamed repo) is accepted, otherwise exit 2 `draft_pr_mismatch` (`details.draft_pr`, `details.pr`). |
| `anchor.head_sha` | string | yes | 40 lowercase hex. The head the findings' lines refer to. |
| `anchor.base_ref` | string | yes | The base branch name the draft was written against. It must follow forgectl's branch grammar (git `check-ref-format`, no leading `-`, no space, `~^:?*[\`, `..`, `@{` or control character; checked by the schema and `draft check`). |
| `anchor.base_sha` | string or null | yes | The base branch tip at drafting time; informational. |
| `anchor.merge_base_sha` | string or null | yes | forgectl `diff --json` `merge_base_sha` at drafting time; used for left-side findings on re-anchor. |
| `event` | string | yes | `comment` or `request_changes`. `approve` is never a draft event; see `--approve`. Changing a posted draft's `event` while keeping its `draft_id` reuses the dedupe key, so the new event does not post; a changed event needs a new draft (`draft new --from`). |
| `body` | string | yes | The review's summary text; may be empty. |
| `findings` | array | yes | At most 500 items; see [Findings](#findings). |
| `unanchored` | array | no | At most 500 together with `findings`, of `{finding, head_sha, reason}`: a finding object as in [Findings](#findings), whose lines refer to `head_sha`, and its [moved reason](#moved-findings). Never mapped or validated; always rendered in the body's "Findings that could not be anchored" section. Adding this optional field keeps `schema_version` 1. |
| `meta` | object | no | Free-form; pr-comment ignores it. The only place extra properties are allowed. |

Every other object in the draft has `additionalProperties: false`. Each multi-line text field (`body`, a finding's `body`, each suggestion) is at most 64 KiB of UTF-8, a `title` at most 256 characters, a `path` at most 4096 bytes; a draft file is at most 16 MiB. The byte caps (64 KiB per text field, 4096 bytes per `path`), the combined 500-item limit and the one-line rule are pr-comment's checks; the schema's `maxLength` counts code points and only approximates them. A violation exits 2 (`invalid_draft`, with `details.errors` listing `{pointer, message}`).

### Findings

| Field | Type | Required | Rule |
|---|---|---|---|
| `id` | string | yes | Unique across the draft's `findings` and `unanchored`. |
| `path` | string | yes | The path as forgectl `files` or `diff` prints it at the anchor head; for both sides the head-side path (the new path of a renamed file). One line, at most 4096 bytes. A path containing a sanitizing marker (`<U+`) cannot be commented inline and moves (`invalid_position`). |
| `line` | integer or null | yes | 1-based. `null` makes a **file-level finding**: it always goes to the review body, and `start_line`, `side` and `suggestion` must be absent. |
| `start_line` | integer | no | `start_line < line`, same side. |
| `side` | string | no | `right` (default; the head's version) or `left` (the base's version at the merge-base). |
| `severity` | string | no | `blocker`, `major`, `minor`, `nit`, `info`. |
| `title` | string | no | One line, at most 256 characters; stripped of leading and trailing whitespace, and an empty title counts as absent. |
| `body` | string | yes | Non-empty. |
| `suggestion` | object | no | `{"lines": [string, ...]}`: replacement code for `start_line..line`, one string per line; see [Suggestions](#suggestions). `lines: []` suggests deleting the range. Only on `side: right`; on `left` the draft is invalid. |

### Draft versioning

- `schema_version` is an integer. pr-comment accepts every version it knows and refuses any other with exit 2 `unsupported_draft_version` (`details.supported`).
- Adding an optional field does not change the version. A field whose meaning changes, or a new required field, makes a new version; pr-comment keeps reading older versions for at least one minor release after a new one ships.
- `pr-comment draft check` validates a draft offline.

## Rendering

### Text pipeline

Every body pr-comment posts is built in this order. Each untrusted input is a text field of a draft (`body`, a finding's `title`, `path`, `body`, each suggestion line) or a body file.

1. **Inputs.** Strict UTF-8 (exit 2 otherwise: `invalid_draft` for a draft, `usage` for a body file). Split on LF, CRLF, lone CR, VT, FF, FS, GS, RS, NEL, LS and PS and rejoin with LF (forgectl, section Neutralizing, step 1). A character of category Cc other than tab and LF, or of category Cs (a lone surrogate from a JSON `\uD800` escape), exits 2 `invalid_text` (`details: {pointer, kind: "control_character"}`). One-line fields (`title`, `path`) and suggestion lines must contain no LF after the split; pr-comment checks this itself after the split (a full-string match, never a search with `$`), else exit 2 `invalid_draft`.
2. **Suggestion fences.** A suggestion fence in untrusted multi-line text exits 2 `invalid_text` (`kind: "suggestion_fence"`) ([Suggestions](#suggestions)).
3. **Characters** (per untrusted part, with the canonical repo's config; never on the lines of an applicable suggestion): [Mentions](#mentions); `<` to `＜` and `!` before `[` to `！` ([Body structure](#body-structure)). Counts are taken from the input before this step.
4. **Parts:** finding text, entries, suggestions and sections are rendered ([Finding text](#finding-text) to [Suggestions](#suggestions)); the attribution line is rendered and passes through Mentions.
5. **Assembly** in the layout of [Body structure](#body-structure): attribution line first, marker last.
6. **forgectl's neutralizing, applied by pr-comment:** every line whose first character is `/` gets one leading space (forgectl, section Neutralizing, step 2), and case-insensitive `forgectl:dedupe:` has its second colon replaced by `-` (forgectl, section Dedupe key). pr-comment counts both as `neutralized_lines` and `markers_removed`.

The result is the body handed to forgectl. The plan shows it followed by the line `<!-- forgectl:dedupe:<key> -->` that forgectl appends; nothing else changes it. If forgectl's result reports `neutralized_lines` or `markers_removed` above 0, pr-comment adds the warning `neutralizing_mismatch` (a forgectl change it does not know), logs it and still exits 0.

### Body structure

pr-comment parses no Markdown. It neutralizes untrusted text by character and puts its own text where nothing untrusted can change it.

- **Layout.** Parts are separated by exactly one blank line, and each untrusted part starts a new block. A review body is: the attribution line, a blank line, `---`, a blank line, the parts, a blank line, the marker line. `comment`, `reply` and `approve` bodies have the same layout with their one part; an approve without a body file is the attribution line, a blank line and the marker. With `footer.human: false` the attribution line and the `---` are left out. An inline comment is its rendered finding text, a blank line and the marker. When there are no parts, the `---` and its blank line are left out.
- **Characters neutralized.** In every untrusted part, except the lines of an applicable suggestion: every `<` becomes `＜` (U+FF1C), and every `!` immediately followed by `[` becomes `！` (U+FF01). With `<` gone, untrusted text can open no raw HTML, HTML comment, `<details>`, `<img>` or angle autolink in any engine; every image form starts with a literal `![`. Character references such as `&lt;` decode to text and are left as they are. This applies to text a human wrote too, as [Mentions](#mentions) does.
- **Why the attribution line comes first.** No construct in untrusted text can change a block before it, so the attribution line always renders as written. Text after an untrusted part can be pulled into a block the part left open (an unclosed fence, GitLab's `>>>` quote): pr-comment's later headings and entry lines, and the marker, then render as visible code or quote text. Marker detection and forgectl's dedupe read raw text, so they are unaffected.
- **Counts.** Over the untrusted parts as given (after Text pipeline step 1, before step 3): `counts.raw_html`, each `<` followed by an ASCII letter, `/`, `!` or `?`; `counts.images`, each `![`; `counts.links`, after decoding character references (as Python `html.unescape`) and removing each backslash before an ASCII punctuation character, each `//` followed by a character other than whitespace and `/`, each `www.` at a word start, each `mailto:`, and each e-mail form (`[A-Za-z0-9._%+-]@[A-Za-z0-9-]+\.[A-Za-z]`); `counts.invisible`, characters forgectl's Sanitizing would mark. Counts include code and are upper bounds (a URL with `www.` counts twice). They are in the plan, the dialog summary and the hook payloads.

### Finding text

An inline comment, or a finding's entry in the review body, is rendered as: a bold prefix when `severity` or `title` is present, a blank line, the finding's `body`, and its suggestion block if any ([Suggestions](#suggestions)).

- **Bold prefix.** `**<severity> · <title>**` with only the present parts; `title` is stripped of leading and trailing whitespace first, and an empty title counts as absent; in `title` every ASCII punctuation character other than `@` is backslash-escaped, so it cannot close the bold or start other syntax.
- **Paths in text** are written as a code span of n backticks, a space, the displayed path, a space, n backticks, where n is one more than the longest backtick run in it. The displayed path is `path` after Text pipeline step 3, with characters forgectl's Sanitizing would mark shown as `<U+XXXX>` markers; then `:L` for a line, `:S-E` for a range, and, for a left-side finding, the word `(left)` after the span, separated by a space. Only the raw path goes to forgectl's `comments[].path`.

### Review body assembly

The review body is laid out as in [Body structure](#body-structure); its parts, in order, each only when non-empty:

1. the draft's `body`;
2. `### File-level findings`: per finding, its entry line (the path as in [Finding text](#finding-text)), a blank line, its rendered text;
3. `### Findings that could not be anchored`: per entry, its entry line as in [Moved findings](#moved-findings), a blank line, its rendered text: every moved finding of this run, then every `unanchored` entry.

The attribution line and the marker are added around the parts by the layout ([Footers and marker](#footers-and-marker)).

### Moved findings

A finding that cannot be posted inline moves to the review body. Its entry line is `` `<path span>` (at `<12-hex sha>`) — <reason text> ``, with the path span as in [Finding text](#finding-text), then a blank line and the rendered finding text, with any suggestion rendered as a plain code block ([Suggestions](#suggestions)). The line and sha shown are the last anchor at which the finding had a known line: B for a finding that mapped and then failed validation at B, else A (for an `unanchored` entry, its `head_sha`). Entries keep draft order in every section, and inline comments keep draft order in forgectl's `comments` list. Example: ```` `src/a.py:10-12` (left) (at 0123456789ab) — the base branch changed after this review was drafted ````.

| Reason | When | Text |
|---|---|---|
| `changed` | [Re-anchoring](#re-anchoring): the finding's line (or a line of its range) was modified or removed between the heads, or the file is binary. | the commented line changed after this review was drafted |
| `file_removed` | Re-anchoring: the file was deleted at B, or renamed in a way the host did not detect. | the file was deleted or renamed after this review was drafted |
| `diff_unavailable` | Re-anchoring: forgectl `compare` returned `diff: null` (`behind`, `diverged`, `unavailable`) or `too_large`, or its diff could not be read unambiguously ([Reading the diff](#re-anchoring)). | the changes since this review was drafted could not be compared |
| `base_changed` | The left-side check ([Re-anchoring](#re-anchoring) step 3), on every review with a `left` finding: the merge-base changed or either merge-base is unknown. | the base branch changed after this review was drafted |
| `invalid_position` | [Position validation](#position-validation): forgectl's pre-check found the line is not a valid position at the pinned head, or the path contains `<U+`. | the line is not part of this pull request's diff |
| `files_incomplete` | [Pre-check](#pre-check): forgectl refused the review as `too_large`, its file list is incomplete and the path is not listed. | the host's file list for this pull request is incomplete |

### Suggestions

- A suggestion line is raw replacement code: no fence and no line terminator or other forgectl separator (VT, FF, FS, GS, RS, NEL, LS, PS); a draft with one is invalid (`invalid_draft`). pr-comment writes the fence.
- An applicable suggestion uses exactly three backticks: GitHub ```` ```suggestion ````, GitLab ```` ```suggestion:-N+0 ```` with `N = line - start_line` (0 without `start_line`). `lines: []` is allowed and suggests deleting the range.
- **Degraded suggestions.** A suggestion is rendered as a ```` ```text ```` block (fence length as below) when: any line contains a character forgectl's Sanitizing would mark (Cc except tab, Cf, Cs, Co, Cn, Zl, Zp and the listed invisible code points) (`invisible_chars`); any line contains a run of three or more backticks or tildes (`fence`); or any line starts with `/` (`slash_line`; forgectl's neutralizing would add a space). The note line before it is `_Suggestion shown as code: it cannot be posted as an applicable suggestion safely._`, or for a moved finding `_Suggestion shown as code: this finding could not be posted inline._`. Degraded suggestions are reported in `suggestions_degraded` as `[{id, reason}]`. A moved finding's suggestion is always shown as code with the moved note; it is not listed in `suggestions_degraded` (the finding is in `moved`).
- **Order and markers.** A degraded suggestion reports the first reason that applies, in the order `invisible_chars`, `fence`, `slash_line`. Every degraded or moved suggestion line shows markable characters as `<U+XXXX>` markers, whatever its reason. Text pipeline step 6 still gives a `/` line in a degraded block its leading space.
- **Fence length** of `text` blocks: backticks, at least three, one more than the longest backtick run in the content. A tilde run in the content does not affect a backtick fence.
- **Suggestion fences in text.** Untrusted text cannot carry its own suggestion: a line of an untrusted multi-line part with a run of three or more backticks or tildes followed by text that, after decoding character references (as Python `html.unescape`), removing backslashes, stripping leading whitespace and ASCII lowercasing, starts with `suggestion` exits 2 `invalid_text` (`kind: "suggestion_fence"`, `details.pointer`). Over-matching is accepted. A human who wants a suggestion in a reply uses the host's web UI.

### Mentions

Text pr-comment posts can come from a model reading third-party content, and it posts under the user's account, so mentions are neutralized by default. This applies to every body pr-comment posts, including text a human wrote, since it cannot tell them apart.

- **Policy.** Config `mentions.policy` is `neutralize` (default) or `allow`; `mentions.allow` lists names that pass through (default empty), a team as `org/team`. A human actor in an interactive run may add names for one run with `--allow-mention <name>` (repeatable). A caller with its own dialog (`--yes`) cannot use `--allow-mention`; it gets `＠` unless the name is in `mentions.allow`.
- **Where.** Over every untrusted part ([Text pipeline](#text-pipeline) step 3: the draft `body`, each finding's `title`, displayed path and `body`, degraded suggestion lines, a body file) and over the attribution line (its template text and placeholder values). Inline code, fenced and indented code blocks are not exempt. The only exception is the lines of an applicable suggestion: pr-comment writes them from the structured field inside its own fence, and they are code on every host by construction, since no suggestion line can close a fence ([Suggestions](#suggestions)) and pr-comment's fence line starts a new block after a blank line.
- **What is replaced.** Each of these is replaced by `＠` (U+FF20): an `@` not immediately preceded by an ASCII letter or digit (`[A-Za-z0-9]`); and each entity form of `@`, wherever it stands: `&#` with optional zeros and `64` not followed by a digit, `&#x` or `&#X` with optional zeros and `40` not followed by a hex digit, and `&commat`, each with its `;` if present (the whole form is replaced). `\@` is replaced like any `@`. What follows the `@` does not matter, so `@&#111;ctocat` and `@\_name` are covered. A part boundary counts as a non-letter before the `@`.
- **Allowed mentions.** With policy `neutralize`, a literal `@` (never an entity form) is kept when its name is allowed. The name N is the longest match of `[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*` after the `@`, with trailing `.` removed. It is allowed only when N equals an entry of the effective allowlist (config `mentions.allow` plus `--allow-mention`), compared in ASCII lowercase, and the text after N is empty, or starts with whitespace or one of `` , ; : ! ? ) ] } ' " * ~ ``, or is one or more `.` followed by whitespace or the end. Anything else is replaced, so `@john.doe` is not covered by an entry `john`, and `@org/team/sub` not by `org/team`.
- **Why this is safe.** `＠` does not link on GitHub. The pass inserts no syntax and depends on no parse, so no difference between hosts' Markdown engines can leave an `@` live outside an applicable suggestion.
- **Cost.** `@` anywhere in untrusted text shows as `＠`: inline code, code blocks (decorators, annotations), URLs such as `https://host/@user`, and prose. Applicable suggestions and e-mail addresses (`a@b.com`: a letter precedes the `@`) are unchanged.
- `mentions.policy: allow` turns the pass off.
- **Reporting.** `mentions_neutralized` lists, for each replaced form, the name N as the allow rule defines it (taken after the replaced `@` or entity form, trailing `.` removed); empty names are not listed; deduplicated in ASCII lowercase, in order of first appearance: the review body first, then inline comments in draft order. `counts.mentions_neutralized` counts replaced forms.

### Footers and marker

The attribution line is the text pr-comment adds to say who posted and how (config keys `footer.human` and `footer.auto`).

- **Marker.** Every body pr-comment posts carries the marker line `<!-- pr-comment:v1 -->` as its last line; forgectl then appends its dedupe line after it. pr-comment appends it in code; no config removes it. Tools that detect pr-comment's content use [Marker detection](#marker-detection).
- **Templates.** Config `footer.human` (a string, or `false`) is used when the actor is `human:`; `footer.auto` (a non-empty string, never `false`) when it is `auto:`. Each template is one line of at most 512 characters, starts with an ASCII letter or `{`, contains no character of category Cc or Cf, none of forgectl's separators (VT, FF, FS, GS, RS, NEL, LS, PS), and no `<`, `` ` ``, `[`, `]` or `\`; otherwise `config_invalid` (`details.key`), at config load. Defaults:
  - human: `Drafted with AI assistance · posted by {user} after review · {sha}`
  - auto: `Posted automatically under policy {policy} · no human review · {sha}`
- **Placeholders** (a closed set; any other `{...}` fails config validation, `config_invalid`). Every placeholder may appear in either template: `{user}` (the actor's name for `human:`, `me.name` for `auto:`), `{policy}` (the actor's name for `auto:`, empty for `human:`), `{sha}` (12 hex of the pinned head, or of `show`'s head for `comment` and `reply`), `{decision_id}` (the first `--decision-id`, or empty), `{tool_version}`. Literal braces are written `{{` and `}}`; a lone `{` or `}` is `config_invalid`.
- **Placeholder values** are printable ASCII by their grammars (actor names, `me.name`, decision ids, 12-hex shas, the version). `{user}`, `{policy}` and `{decision_id}` are written as a code span (one backtick each side; their grammars contain no backtick) when non-empty, so a value such as `www.example.org` or `1.` cannot autolink or start a block; an empty value is written as nothing. `{sha}` and `{tool_version}` are written as is. The rendered line then passes through [Mentions](#mentions). Ids from hook stdout never enter `{decision_id}`: the plan is built before any hook runs.
- **Rendering.** As in [Body structure](#body-structure).
- **Placement.** `review`: the attribution line at the top of the review body, the marker at its end (so a review always has a body). Every inline comment, on both hosts, ends with the bare marker line (no attribution text). `comment`, `reply` and `approve`: attribution line at the top, marker at the end.
- Footer settings can be overridden per repo (`repos.<id>.footer`).

### Marker detection

A comment, note or review body was posted through pr-comment when both hold:

- it is authored by the authenticated account (compared by id, as forgectl does); and
- after removing trailing whitespace and at most one last line matching `^<!-- forgectl:dedupe:[A-Za-z0-9._:-]{1,128} -->$`, its last non-empty line is exactly `<!-- pr-comment:v1 -->`.

A body that only quotes the marker elsewhere does not match. Read GitHub review bodies from forgectl `reviews`, and inline and general comments and GitLab notes from forgectl `comments`.

### Dedupe keys

pr-comment passes a `--dedupe-key` on every post so a retry never duplicates text (forgectl, section Dedupe key). Keys are deterministic:

| Action | Key |
|---|---|
| `review`, event `comment` or `request_changes` | `prc:rv:<draft_id>` |
| `review --approve` | `prc:ra:<draft_id>:<first 12 hex of the pinned head>` |
| `comment` | `prc:comment:<h>`, `h` over `[pr_id, "comment", body]` |
| `reply` | `prc:reply:<h>`, `h` over `[pr_id, "reply", to, body]` |
| `approve` | `prc:approve:<h>`, `h` over `[pr_id, "approve", head, base, body]` |

- `body` is the given text after [Text pipeline](#text-pipeline) step 1, `""` for an approve without `--body-file`. `head` and `base` are the pinned head sha and base ref. The attribution line, placeholders, mention settings and the tool version do not enter a key, so a retry under a new decision id, config or version reuses it.
- The hash input is `json.dumps(array, ensure_ascii=False, separators=(",", ":"))` encoded as UTF-8; `h` is the first 20 hex of its sha256. Test vector: `["PR_kwDOA", "comment", "hi"]` gives `h` = `fdacdffe9dad6cfe01a5`.
- A caller may replace the key with `--dedupe-key <k>` (forgectl's grammar `^[A-Za-z0-9._:-]{1,128}$`).
- The `review` key omits the anchor, so re-anchoring a draft that already posted reports `already_posted` instead of posting again, and so does an edited draft that keeps its `draft_id`. A new review is a new draft with a new `draft_id`.
- `review --approve` has its own key with the pinned head and posts every finding again under an approving review. To approve after a draft was posted, use [`approve`](#approve); `review --approve` is for posting findings and approval together.
- Changing a posted draft's `event` while keeping its `draft_id` reuses the key: forgectl then posts no text and only applies the new state (GitLab), or exits 12 `outcome_partial` with `retry: new_key` (GitHub). A changed event needs a new draft (`draft new --from`).
- Identical text to the same target is deduplicated by design: posting it again needs a caller `--dedupe-key`; human output says `already posted`.
- **Was it posted?** Rerun the same command (for a review after a push, with `--reanchor-to <head>`): the key makes it post nothing again, and its result's `already_posted` is the answer. A `--dry-run` reports `already_posted` only as a hint: `true` when forgectl's dry run reported it, else `null`. `review` and `approve` refuse a closed or merged PR (`not_open`) before any lookup; nothing more can be posted there. An approval's key includes the head, so after a push a rerun approves the new head (a new action).
- `already_posted: true` means forgectl found the key, not that this exact text is on the host; check `ids` if it matters. Pending reviews and drafts are not searched, and two simultaneous runs can both post (forgectl, section Dedupe key; [Residual risks](#residual-risks)).

## Re-anchoring

`review --reanchor-to <sha>` moves a draft's findings from `anchor.head_sha` (A) onto the PR's current head B. It is deterministic and never guesses.

1. When B equals A, `compare` is not called and right-side findings keep their lines. Otherwise pr-comment runs `forgectl compare <pr> --from A --to B`; its `to_sha` must equal B (else exit 20 `stale_head`, `details.head_sha`). `diff: null` or `too_large` moves every right-side finding (`diff_unavailable`). Left-side findings follow step 3 only.
2. **Right-side findings** are mapped through the A→B diff, file by file:
   - **Reading the diff.** The diff is split into file sections at lines starting with `diff --git` and a space. A section's paths come only from its `---` and `+++` lines (or `rename from` and `rename to` lines), each prefix followed by a space, with git's C-style quoting decoded, a trailing tab and the `a/` and `b/` prefixes removed, and `/dev/null` meaning added or deleted; never from the `diff --git` line. A section with hunks whose paths cannot be read this way, or two sections naming one path, moves every right-side finding (`diff_unavailable`). A section with no hunks and no `Binary files` line is a mode change or pure rename: lines keep their numbers. A `Binary files ... differ` line moves its findings (`changed`). A deleted file (`+++ /dev/null`) moves them (`file_removed`). Paths are compared as forgectl prints them (sanitized on both sides).
   - A file renamed in the diff is followed to its new path.
   - A file absent from the diff is unchanged: its lines keep their numbers.
   - Within a changed file, hunks are read by their header counts (an omitted count is 1). A line of A that the diff removes is unmappable (`changed`); every other line, including a context line shown inside a hunk, maps to A's line plus the net number of lines added minus removed before it. A range finding maps only when every line in `start_line..line` maps and the mapped lines stay contiguous; otherwise it moves (`changed`).
3. **Left-side check**, run by every `review` with at least one left-side finding, re-anchored or not. Left-side findings refer to the base file at the merge-base. pr-comment reads `forgectl diff <pr> --json`; its `head_sha` must equal the pinned head (else exit 20 `stale_head`). An exit 20 `too_large` makes the merge-base unknown. Left-side findings keep their lines only when `anchor.merge_base_sha` and the read's `merge_base_sha` are both known and equal; otherwise they move (`base_changed`). With `--reanchor-to`, a left-side finding whose file the A-to-B diff deletes moves (`file_removed`), and one whose file it renames follows the new path. Without a left-side finding, `diff` is not read.
4. File-level findings follow a detected rename and are never validated. Re-anchored inline findings then go through the [pre-check](#pre-check) at B like any other.
5. `reanchored_draft` is the draft with `anchor` set to B (`head_sha`, `base_ref`, `base_sha` from `show`, `merge_base_sha` from step 3, or `null` when step 3 did not run), `findings` holding every finding that mapped (with new paths and lines, also when position validation at B then moved it) and every file-level finding (path following a detected rename), and `unanchored` holding the source's `unanchored` entries plus every finding that did not map (with `head_sha` A and its reason). It is returned only on exit 0, a dry run included; on any other exit there is no result document. pr-comment writes no file; the caller decides whether to keep it.

`--reanchor-to` takes a full 40-hex sha, uppercase lowered first; it must equal the PR's current head. Line terminators are not significant to the mapping: a CR before LF is part of a line's content, and a `\ No newline at end of file` marker is not a line.

## Pre-check

After rendering, pr-comment runs the forgectl command the real run will use, with `--dry-run` instead of `--yes` (same `--expected-head`, `--expected-base`, `--dedupe-key`, `--actor`, `--decision-id`), and passes the forgectl input on stdin ([forgectl edge](#forgectl-edge)). forgectl then applies its own usage and size checks, state and pin refusals, self-approval, the `--to` and thread lookup, position validation, the dedupe lookup and its GitLab drafts check, and writes nothing.

- exit 0: the plan passed. forgectl's dedupe answer in a dry run is not part of its contract when a state step is still needed, so it never ends the command: the real call decides. If forgectl reported `already_posted: true`, the dialog summary says "forgectl reports this may already be posted; confirming posts nothing again and completes any pending approval or change request".
- exit 12 `invalid_position` (review): the findings at `details.invalid[].index` move with reason `invalid_position`, the plan is rebuilt and the pre-check runs once more; a second `invalid_position` exits 12 `invalid_position`.
- exit 20 `too_large` (review): pr-comment reads `forgectl files <pr>`; if `files_complete` is false and a finding's path is not listed, those findings move with reason `files_incomplete` and the pre-check runs once more; otherwise exit 20 `too_large` passes through.
- exit 2 `body_too_large`: passes through with `details.role` added (`body`, or `inline:<finding id>` mapped from `details.index`).
- any other non-zero exit stops the command with that error, before the dialog.

forgectl's dry-run exit 20 prints a plan with `would_refuse` instead of an envelope; pr-comment reads `would_refuse.reason`, `message` and `details` and exits 20 with its envelope (`reason` and `details` copied, `lower_error: {code: "refused", reason, message, details}`), on a dry run and a real run alike. Rounds run in a fixed order: at most one `too_large` round, then at most one `invalid_position` round; any later refusal passes through. Each round passes its input on stdin; nothing is kept between rounds.

The plan the dialog shows is the one that passed. The real call carries exactly that input, byte for byte from the confirmed plan in memory. If the real call exits 12 `invalid_position` (nothing was posted), pr-comment exits 12 `invalid_position` and moves nothing after confirmation. A pre-check round is logged (`precheck`).

### Position validation

forgectl validates positions (forgectl, section Position validation). pr-comment adds only: a path containing `<U+` moves before the pre-check (`invalid_position`); file-level findings are never validated and always post.

## Commands

All commands accept the [global flags](#global-flags). Posting commands are confirm-by-default with no pending store (architecture section 12.4): see [Confirmation](#confirmation).

### `review`

`pr-comment review <pr> --draft-file <path>|- [--approve] [--reanchor-to <sha>] [--dedupe-key <k>] [--allow-mention <name>]...`

Posts one review from a draft, following the [Review order](#review-order). The event is the draft's `event`. `--approve` posts an approving review instead: the draft's findings, body and attribution line still post, and the [automatic approval guard](#automatic-approval-guard) applies. `--approve` with a draft whose `event` is `request_changes` exits 2 `approve_conflicts_event`, for every actor. `--reanchor-to` is described in [Re-anchoring](#re-anchoring).

### `comment`

`pr-comment comment <pr> --body-file <path>|- [--expected-head <sha>] [--dedupe-key <k>] [--allow-mention <name>]...`

Posts a general PR comment, following the [Comment and reply order](#comment-and-reply-order). Allowed on closed and merged PRs.

### `reply`

`pr-comment reply <pr> --to <id> --body-file <path>|- [--expected-head <sha>] [--dedupe-key <k>] [--allow-mention <name>]...`

Replies to a comment, thread or review (`gh:ic:`, `gh:rc:`, `gh:rt:`, `gh:r:`, `gl:d:`, `gl:n:`), following the [Comment and reply order](#comment-and-reply-order). Allowed on closed and merged PRs. An unknown `--to` id exits 10 at the pre-check, before the dialog. `threaded: false` in the output means the host had no thread to join (a reply to a GitHub general comment or review body posts a new general comment; forgectl, section Identifiers); human output says so.

### `approve`

`pr-comment approve <pr> [--body-file <path>|-] [--expected-head <sha>] [--expected-base <ref>] [--dedupe-key <k>] [--allow-mention <name>]...`

Approves the PR at a pinned head and base, following the [Approve order](#approve-order). Outside an interactive run both `--expected-head` and `--expected-base` are required (exit 2 otherwise).

### `resolve` and `unresolve`

`pr-comment resolve|unresolve <pr> --thread <id> [--thread <id>]...`

Resolves or unresolves at most 100 review threads (`gh:rt:` or `gl:d:` only; forgectl, section Commands, `resolve` and `unresolve`), following the [Resolve order](#resolve-order).

### `draft check`

`pr-comment draft check <path>|-`

Validates a draft offline against [schemas/review-draft.json](../schemas/review-draft.json) and the rules of [Review draft](#review-draft) that need no host (ids unique, `line`/`start_line`/`side`/`suggestion` consistency, caps, version). It applies [Text pipeline](#text-pipeline) steps 1 and 2 and reads no repo config. Exit 0 valid, 2 invalid, with the result document either way ([Other results](#other-results)). It runs no forgectl and writes no audit record.

### `draft new`

`pr-comment draft new <pr> [--event comment|request_changes] [--from <path>|-]`

Reads `forgectl diff <pr> --json`, then `forgectl show <pr>`; if their heads differ it reads both once more, then exits 20 `stale_head`. When `diff` exits 20 `too_large`, `merge_base_sha` is `null`, a warning goes to stderr and the log, and the head comes from `show` alone (left-side findings added later then move as `base_changed`, [Re-anchoring](#re-anchoring) step 3). Prints a draft to stdout ([schemas/cli/draft-new.json](../schemas/cli/draft-new.json)): `schema_version` 1, a new `draft_id` (a ULID: 48-bit millisecond time and 80 random bits, Crockford base32), `pr` from `show`, `anchor` (`head_sha` and `base_sha` from `show`, `base_ref` from `show`, `merge_base_sha` from `diff`), `event` (default `comment`), `body` `""`, `findings` `[]`.

With `--from`, `event`, `body`, `findings`, `unanchored` and `meta` are copied from that draft, which must be valid and whose `anchor.head_sha` must equal the current head (else exit 20 `stale_head`): the way to post a new review of the same findings. The source draft's `pr` is matched as in [Draft fields](#draft-fields) (`draft_pr_mismatch`); `--from -` with stdin a TTY exits 2 (`usage`). Left-side findings are copied unchanged, since the head is equal; the left-side check applies when they post. `--event` overrides the copied event. No write, no hooks, no audit record.

### `export`

`pr-comment export [--since <time>] [--log]`

Prints audit records (or, with `--log`, log records) as JSONL, oldest first; with `--json`, one JSON array ([schemas/cli/export.json](../schemas/cli/export.json)). `--since` takes RFC 3339 or a duration (`7d`). A missing file prints nothing (or `[]`); a malformed line is warned on stderr and skipped.

## Global flags

| Flag | Meaning |
|---|---|
| `--json` | Machine output on stdout ([Output](#output)); the confirmation dialog still uses stderr. |
| `--config <path>` | Config file instead of `~/.config/pr-comment/config.yaml` (drop-ins are read next to it). |
| `--dry-run` | Build the plan and run the [pre-check](#pre-check); send nothing, run no hooks, write no audit record. Exit 0 prints the result with the plan; a refusal exits 20 with the error envelope, like any other failure. Needs no `--yes` or TTY, but the same flags as a real run. |
| `--yes` | Skip the confirmation dialog. Never overrides a refusal. See [Confirmation](#confirmation). |
| `--expect-plan <sha256>` | Refuse, exit 20 `plan_changed` (`details.plan_sha256`), before the dialog and any write, when the final plan's `plan_sha256` differs. A caller that showed a human or gate pr-comment's `--dry-run` plan passes its hash with `--yes`. Posting commands only; with `resolve`, `unresolve`, `draft check`, `draft new` or `export` it exits 2 (`usage`). Checked after the pre-check and before the dry-run output, so `--dry-run --expect-plan <h>` refuses `plan_changed` too. |
| `--override-escalation=<scope>` | Human-only, repeatable; `scope` is `pr-comment` or `forgectl`, as in the envelope's `escalated_by`. `pr-comment` counts exit 13 from pr-comment's own `pre-post` hooks as allow; `forgectl` passes `--override-escalation` to every forgectl write. A veto still vetoes. Without a value: exit 2 (`usage`). A run that escalates in pr-comment's hooks and then, after confirmation, in forgectl's needs a second confirmation, which passes both scopes. Recorded in audit whenever given. |
| `--actor <actor>` | See [Actors and interactive runs](#actors-and-interactive-runs). |
| `--decision-id <id>` | Repeatable, at most 32, grammar `^[A-Za-z0-9._:@/-]{1,128}$`. Recorded in audit and passed to forgectl. |
| `--hook <name>=<cmd>` | Per-invocation hook, run after configured hooks ([Hooks](#hooks)). |
| `--verbose`, `--quiet` | More or less human output on stderr. |
| `--version` | Print the version. |

## Review order

`review` runs these steps in order. A step that fails stops the command with nothing sent unless stated.

1. **Usage and draft** (offline, config-free checks only): flag rules (human-only flags; `--allow-mention` interactive only; `-` with a TTY stdin exits 2; forwarded values validated, see [forgectl edge](#forgectl-edge)); open the draft without following symlinks, non-blocking, regular file or stdin, at most 16 MiB, strict UTF-8; parse; schema, version, caps, consistency; Text pipeline steps 1 and 2; `--approve` with `event: request_changes` exits 2 `approve_conflicts_event`. Check `audit.jsonl` can be appended and the temporary directory is safe ([Audit and log](#audit-and-log), [Files](#files)). Errors exit 2 (12 for the audit and directory checks).
2. **Read:** `forgectl show <pr>`. Draft `pr` match ([Draft fields](#draft-fields)). State not `open`: exit 20 `not_open` (`details.state`). Without `--reanchor-to`, a head other than `anchor.head_sha` exits 20 `stale_head` (`details.head_sha`; the message says the head moved and to rerun with `--reanchor-to <head>`, which the key makes safe); pr-comment never re-anchors on its own. With `--reanchor-to <B>`, a head other than B exits 20 `stale_head`. A base ref other than `anchor.base_ref` exits 20 `stale_base` (a retarget changes the whole diff; it needs a new draft; a changed base tip is fine). Pinned head: `anchor.head_sha` or B; pinned base: `anchor.base_ref`. With `--approve`, the [automatic approval guard](#automatic-approval-guard).
3. **Re-anchor and left-side check:** with `--reanchor-to`, [Re-anchoring](#re-anchoring) steps 1 and 2 (no `compare` when B equals A); with any left-side finding, step 3.
4. **Render:** Text pipeline steps 3 to 6 with the canonical repo's config (the character step), dedupe key, plan and `plan_sha256`.
5. **Pre-check:** [Pre-check](#pre-check), input on stdin; at most one `too_large` round, then at most one `invalid_position` round, each rebuilding step 4. It never ends the command on `already_posted`.
6. **Plan binding:** `--expect-plan` differing from `plan_sha256`: exit 20 `plan_changed`.
7. **Dry run:** with `--dry-run`, print the result with the plan, exit 0.
8. **Confirm:** [Confirmation](#confirmation) (one dialog, covering the follow-ups).
9. **Post:** `pre-post` hooks for this write (veto 11, escalation 13 unless the scope `pr-comment` of `--override-escalation` counts it as allow; before the first write these go to the log only), then audit `attempted` (first write only), then `forgectl review <pr> --file=- --expected-head=<pinned head> --expected-base=<pinned base> --dedupe-key=<key> --json --yes --actor=<actor> [--decision-id=<id>]... [--override-escalation]` (the last only with scope `forgectl`), the confirmed plan on stdin, then `post-post`. A push after step 2 makes forgectl refuse (`stale_head`), passed through. `invalid_position` here exits 12 with nothing posted. forgectl's `already_posted` is reported as is.
10. **Follow-up (GitLab):** [GitLab review body thread](#gitlab-review-body-thread) (thread from the post's `thread_ids`, or from `ids` after `already_posted`), with its own `pre-post`, write and `post-post`.
11. **Finish:** audit outcome (with `writes`), output.

## Approve order

1. **Usage:** as Review order step 1 for flags; the body (if `--body-file`) as in [Comment and reply order](#comment-and-reply-order) step 1, except that a body file whose content is empty or only whitespace after Text pipeline step 1 counts as no body file (the key's `body` is `""`). Outside an interactive run, `--expected-head` and `--expected-base` are required (exit 2).
2. **Read:** `forgectl show <pr>`; state not `open` exits 20 `not_open`; in an interactive run, missing `--expected-head` and `--expected-base` are pinned from this read and shown in the dialog; a head or base different from the expected values exits 20 `stale_head` or `stale_base`; the [automatic approval guard](#automatic-approval-guard).
3. **Render:** Text pipeline steps 3 to 6 (the attribution line, the body if any, the marker), so every approval carries the marker; dedupe key ([Dedupe keys](#dedupe-keys)); plan.
4. **Pre-check, plan binding, dry run, confirm, post** (`forgectl approve <pr> --body-file=- --expected-head=<h> --expected-base=<b> --dedupe-key=<key> --json --yes --actor=<actor> [--decision-id=<id>]... [--override-escalation]`, the last only with scope `forgectl`), **finish:** as Review order steps 5 to 9 and 11.

On GitLab an approval with a body is a note followed by the approval, so a failure in between exits 12 `outcome_partial` ([Error envelope](#error-envelope) gives `landed` and `retry`); a rerun reuses the key and completes only the approval. `head_verified: false` (an earlier approval exists that forgectl cannot confirm counts for this head and base) is reported, and human output says so.

## Comment and reply order

1. **Usage; the body:** as the draft in Review order step 1, at most 64 KiB, non-empty, Text pipeline steps 1 and 2; `--to` and `--expected-head` validated.
2. **Read:** `forgectl show <pr>`. With `--expected-head`, a different head exits 20 `stale_head` (`details.head_sha`). The head shown and recorded is the expected or `show`'s head. Hosts enforce no head on a comment, so a push between this read and the post goes undetected ([Residual risks](#residual-risks)).
3. **Render:** Text pipeline steps 3 to 6, dedupe key ([Dedupe keys](#dedupe-keys)), plan.
4. **Pre-check** (an unknown `--to` exits 10, `details.what: to`), **plan binding, dry run, confirm, post** (`forgectl comment <pr> --body-file=-` or `forgectl reply <pr> --to=<id> --body-file=-`, each with `--dedupe-key=<key> --json --yes --actor=<actor> [--decision-id=<id>]... [--override-escalation]`, the last only with scope `forgectl`), **finish:** as Review order steps 5 to 9 and 11.

## Resolve order

1. **Usage:** each `--thread` id is `gh:rt:` or `gl:d:` in forgectl's form (exit 2 otherwise); at most 100 ids (exit 2 `usage`); duplicates are dropped.
2. **Read:** `forgectl show <pr>` first (no state rule; its `pr`, `pr_id` and `repo` are used for hooks, audit and `repos.<id>`, and the host is `repo` up to its first `/`), then `forgectl comments <pr>`, then `forgectl whoami --host <host>`. A `whoami` that fails, exits 12 `partial_results`, or has a null `id` for the host is not fatal: authors show as "unknown", `thread_author_is_self` is null, and a `warning` is logged. Per thread: absent from a `complete: true` listing gives that thread `error` `not_found` (`details.what: thread`); absent from a `complete: false` listing stays to change, with its author, path and excerpt unknown, and forgectl's own lookup decides (exit 10 is that thread's `not_found`; already in the requested state is a forgectl no-op, reported `changed: null`); `resolvable: false` gives `not_resolvable` (exit 20); already in the requested state gives `changed: false` and no write.
3. **Dry run:** with `--dry-run`, print the result with each thread's intended change; exit 0 when no thread has an error, else the first failing thread's exit code.
4. **Confirm:** one dialog listing every thread to change with its first author (you, another account, or unknown), `path:line` and the first 200 characters of its first comment, display-sanitized ([Human output](#human-output)). No thread to change: no dialog, no hooks, no audit record; exit 0 with the result (or the first thread error's exit). Declined: exit 13.
5. **Per thread to change**, in the given order: `pre-post` (a veto or escalation is that thread's error, and the next thread continues), audit `attempted` (first write that reaches forgectl), `forgectl resolve|unresolve <pr> --thread=<id> --json --yes --actor=<actor> [--decision-id=<id>]... [--override-escalation]` (the last only with scope `forgectl`), `post-post`.
6. **Finish:** audit outcome with `writes` (when any write was tried), output. Exit 0 when every thread succeeded; else the first failing thread's exit code and error, with the result document on stdout.

## Confirmation

- **Interactive runs** show the plan on stderr and ask `Post? [y/N]`. The dialog prints the full plan, then a summary just above the prompt: the action and event in capitals (`REVIEW: REQUEST_CHANGES`, `APPROVE`), the PR, pinned head and base, counts (inline, moved by reason, file-level, findings by severity, suggestions degraded, mentions neutralized, links, images, raw HTML (these three are upper bounds), invisible characters marked), the note that `<` and `![` were neutralized, allowed mentions, the pre-check's `already_posted` hint when forgectl reported one ([Pre-check](#pre-check)), and the follow-ups. With `--approve` or `approve` at a head other than `anchor.head_sha`, it says `APPROVING <B12>; FINDINGS DRAFTED AT <A12>`. The answer is read from the terminal, trimmed and compared case-insensitively with `y` and `yes`; anything else, EOF or a read error declines: exit 13 `confirmation_required` (message `declined`) with nothing sent and no audit record (logged as `declined`).
- **Non-interactive runs without `--yes`** exit 13 `confirmation_required` before any write, without recording anything: pr-comment has no pending store, and its caller records the pending confirmation (architecture section 12.4).
- **`--yes`** skips the dialog. Callers pass it only when a human accepted the action, a config opt-in enables it, or an enforced gate allows it (architecture section 12.4). It never overrides a refusal. A caller that showed pr-comment's `--dry-run` plan passes `--expect-plan` with it.
- **One confirmation.** After its own confirmation, pr-comment always calls forgectl with `--yes`, the actor and the decision ids, so forgectl never prompts a second time. forgectl's checks, hooks, neutralizing and refusals still apply and pass through.

## Human output

Every untrusted string pr-comment shows on a terminal (the dialog, `--dry-run` human output, messages, `hook_stderr`, forgectl messages) is displayed with forgectl's Sanitizing rules: Cc except tab and LF, Cf, Cs, Co, Cn, Zl, Zp and forgectl's listed invisible code points become `<U+XXXX>`; one-line fields also mark tab and LF. The posted text is not changed by display (C0 and C1 controls are refused earlier, [Text pipeline](#text-pipeline)). `--json` output is not display-sanitized; its untrusted fields are marked in the schemas.

## Automatic approval guard

An approving action (`approve`, or `review --approve`) by an `auto:` actor exits 20 `auto_approve_disabled` unless all hold:

- config `allow_auto_approve` is `true` for the PR's repo (global or `repos.<id>`; default `false`);
- at least one `--decision-id` is given;
- the pinned head equals `anchor.head_sha`: `review --approve --reanchor-to <B>` with B other than A is refused for an `auto:` actor, exit 20 `auto_approve_reanchored`, since the gate and the draft saw A, not B.

Only the caller's `--decision-id` flags count: ids a hook prints are recorded in `hook_decision_id` but never satisfy the guard, which runs before any hook. A gate used for automatic approval is run by the caller, which passes its decision id.

Human actors are never affected. When the pinned head differs from `anchor.head_sha`, the dialog summary says `APPROVING <B12>; FINDINGS DRAFTED AT <A12>`. Automatic comments and reviews have no extra rule here; the `auto` attribution line is mandatory for them ([Footers and marker](#footers-and-marker)).

## GitLab review body thread

On GitLab a published review body is a resolvable discussion, which on projects requiring resolved threads blocks the merge (forgectl, section Threads and comments).

- Config `gitlab.resolve_body_thread` is `non_blocking` (default: after `comment`-event and approving reviews), `never` or `always` (also after `request_changes`), per repo overridable. Inline finding threads are never resolved automatically.
- **Finding the thread.** After a fresh post: one `forgectl comments <pr>` read; the body thread is the thread among the review's `thread_ids` whose first comment's last non-empty line is `<!-- forgectl:dedupe:<this key> -->`. After `already_posted` (the real call): the thread containing the comment in forgectl's `ids`. If it is resolvable and unresolved, it is resolved; already resolved is `body_thread_resolved: true` with no write.
- The resolve runs `forgectl resolve <pr> --thread=<gl:d:...> --json --yes --actor=<actor> [--decision-id=<id>]... [--override-escalation]` (the last only with scope `forgectl`). It is part of the confirmed plan (`follow_ups`), with its own `pre-post` and `post-post` runs (`action: resolve`) and an entry in the audit record's `writes`.
- A failure, veto or escalation of the resolve, or a read-back with no `thread_ids`, still exits 0 (the review landed) with `warnings: ["body_thread_unresolved"]` and `body_thread_resolved: false`. On GitHub, and when the setting does not apply, `body_thread_resolved` and `body_thread_id` are `null`.

## Output

With `--json`, stdout carries one JSON document: on exit 0 the command's result (schemas in [schemas/cli/](../schemas/cli/)), otherwise the [error envelope](#error-envelope). Exceptions to "non-zero prints the envelope": `resolve` and `unresolve` print their result document with per-thread errors; `draft check` prints its result on exit 2 too (`valid: false`). Human output goes to stderr ([Human output](#human-output)) and may change freely. All text fields that come from a draft, a body or a host are marked untrusted in the schemas.

### Post result

`review`, `comment`, `reply` and `approve` print a post result ([schemas/cli/review.json](../schemas/cli/review.json), [comment.json](../schemas/cli/comment.json), [reply.json](../schemas/cli/reply.json), [approve.json](../schemas/cli/approve.json)):

| Field | Commands | Meaning |
|---|---|---|
| `pr`, `action`, `dry_run` | all | `action` is the command name; `review --approve` reports `review` with `event: approve`. |
| `event` | review, approve | `comment`, `request_changes` or `approve`; `approve` reports `approve`. |
| `draft_id` | review | From the draft. |
| `to` | reply | The `--to` id. |
| `dedupe_key` | all | The key passed to forgectl. |
| `head_sha`, `base_ref` | all | Pinned values; for `comment` and `reply`, the `--expected-head` value or `show`'s head, and `show`'s base ref. Never null. |
| `plan_sha256` | all | The final plan's hash ([Posting plan](#posting-plan)), on every run. |
| `posted_inline` | review | Finding ids posted as inline comments. |
| `moved` | review | `[{id, reason}]` for findings moved in this run, reasons as in [Moved findings](#moved-findings). |
| `file_level` | review | File-level finding ids. |
| `suggestions_degraded` | review | `[{id, reason}]`, `reason` in `invisible_chars`, `fence`, `slash_line` ([Suggestions](#suggestions)); moved findings are excluded (they are in `moved`). |
| `mentions_neutralized` | all | Names after a replaced `@`, without it ([Mentions](#mentions)). |
| `neutralized_lines`, `markers_removed` | all | pr-comment's counts from Text pipeline step 6. |
| `ids`, `thread_ids`, `review_id`, `already_posted`, `follow_up_done`, `head_verified`, `threaded`, `warnings` | as in forgectl | Passed through from forgectl's result (forgectl, section Commands); `threaded` only on `reply`, `head_verified` on `review` and `approve`. `warnings` also carries pr-comment's own (`body_thread_unresolved`, `neutralizing_mismatch`). forgectl's `ids` are unordered, so no finding-to-comment mapping is promised. |
| `body_thread_resolved` | review | `true`, `false` or `null` ([GitLab review body thread](#gitlab-review-body-thread)). |
| `body_thread_id` | review | The GitLab body thread (`gl:d:` id) or `null`. |
| `reanchored_draft` | review | The re-anchored draft ([Re-anchoring](#re-anchoring) step 5), or `null` without `--reanchor-to`. |
| `plan` | all | `{plan_sha256, bodies: [{role, text, bytes}], inline_comments: [{finding_id, path, line, start_line, side, role}], commands: [<forgectl argv>], follow_ups: [{action, thread_id}]}` on a dry run; `null` otherwise. `role` is `body` or `inline:<finding id>`; a follow-up's `thread_id` is `null` when it is known only after the post; `commands` lists the real forgectl calls (with `--yes`), not the pre-check. |

`follow_up_done` is forgectl's (it performed a state step on a dedupe hit); `plan.follow_ups` are pr-comment's own follow-up writes (the GitLab body-thread resolve).

On a `--dry-run`:

| Field | Value |
|---|---|
| `ids`, `thread_ids` | `[]` |
| `review_id`, `follow_up_done`, `head_verified`, `body_thread_resolved`, `body_thread_id` | `null` |
| `already_posted` | forgectl's dry-run value when it reported `true`, else `null` (a hint) |
| `threaded` | forgectl's computed value |
| `neutralized_lines`, `markers_removed` | pr-comment's counts |
| `warnings` | pr-comment's |

### Resolve result

`resolve` and `unresolve` print `{pr, action, dry_run, threads: [{thread_id, resolved, changed, error}]}` ([schemas/cli/resolve.json](../schemas/cli/resolve.json), [unresolve.json](../schemas/cli/unresolve.json)); `error` is `null` or an error object as in the envelope. `changed` is true, false, or null: `false` for a thread pr-comment found already in the requested state (no write); `null` for a thread pr-comment could not see before the write (a listing that was not complete), or on failure. With a failure, `--json` stdout still carries this document, and the exit code and error are the first failing thread's ([Output](#output)). On a dry run, `resolved` and `changed` are the intended change.

### Other results

`draft check` prints `{valid, schema_version, draft_id, findings, errors}` ([schemas/cli/draft-check.json](../schemas/cli/draft-check.json)) on exit 0 and exit 2: `errors: [{pointer, message, kind}]` (`kind` is the `invalid_text` kind, null for schema errors); `schema_version` and `draft_id` null when unreadable; `findings` is the count. `draft new` prints a review draft ([schemas/cli/draft-new.json](../schemas/cli/draft-new.json)). `export` prints JSONL records ([schemas/audit.json](../schemas/audit.json), [schemas/log.json](../schemas/log.json)), or with `--json` one array of them ([schemas/cli/export.json](../schemas/cli/export.json)).

## Exit codes

Codes 0–21 follow design guide section 4.2. pr-comment defines no codes of 22 or above.

| Exit | Meaning | Codes and reasons |
|---|---|---|
| 0 | Success, including `already_posted` and a dry run. | |
| 1 | Internal error. | `internal` |
| 2 | Usage. | `usage`, `config_invalid`, `invalid_pr`, `invalid_draft`, `invalid_text`, `unsupported_draft_version`, `draft_pr_mismatch`, `approve_conflicts_event`, `body_too_large` (forgectl's, from the pre-check) |
| 10 | Not found: PR, reply target or thread. | `not_found` |
| 11 | Vetoed by a `pre-post` hook (pr-comment's or forgectl's). | `vetoed` |
| 12 | Failed for a reportable reason. | own: `tool_missing`, `lower_contract_mismatch`, `audit_unavailable`, `audit_failed`, `state_unsafe`, `invalid_position` (after the second pre-check round, or from the real call); forgectl's, passed through: `outcome_unknown`, `outcome_partial`, `quick_action_ran`, `approved_stale_head`, `auth_required`, `permission_denied`, `rate_limited`, `upstream_error`, `network_error`, `host_rejected`, `unsupported_host_version`, `wrong_host_response`, `audit_failed` |
| 13 | Needs a human. | `confirmation_required`, `escalated` |
| 20 | Refused; `reason` required. | own: `stale_head`, `stale_base`, `auto_approve_disabled`, `auto_approve_reanchored`, `not_open`, `plan_changed`, `not_resolvable`; forgectl's, passed through: `stale_head`, `stale_base`, `not_open`, `self_approval`, `drafts_present`, `pending_review_exists`, `not_resolvable`, `too_large`, `unknown_host` |

- forgectl's exit 2 passes through only for `body_too_large`, `invalid_pr` and `config_invalid`; any other forgectl exit 2 is pr-comment building a call forgectl rejects, and exits 12 `lower_contract_mismatch`.
- `drafts_present` (GitLab: the account has unpublished draft notes on the MR) and `pending_review_exists` (GitHub: the account has a pending review) need the user to finish or delete that review in the web UI; human output says so.
- After exit 12, follow the envelope's `retry` ([Error envelope](#error-envelope)). pr-comment never retries a write itself.

### Error envelope

`{"error": {"code", "message", "reason", "details", "retry_after", "landed", "retry", "lower_error", "hook", "hook_stderr", "escalated_by", "escalated_hooks", "decision_ids"}}` ([schemas/cli/error.json](../schemas/cli/error.json)); only `code` and `message` are required.

- Exit 20 has `code: refused` with `reason` required; "exits 20 `stale_head`" means that reason.
- When forgectl fails, pr-comment exits with forgectl's exit code, copies its `code`, `reason` and `details`, and carries forgectl's whole envelope in `lower_error`. `retry_after` (seconds) is copied from forgectl's envelope.
- `landed` and `retry` on every exit 12 follow this table; other exits have both null.

  | Exit-12 code | `landed` | `retry` |
  |---|---|---|
  | `tool_missing`, `audit_unavailable`, `state_unsafe`, `invalid_position`, `auth_required`, `permission_denied`, `rate_limited`, `upstream_error`, `network_error`, `unsupported_host_version`, `wrong_host_response` | `false` | `same_command` |
  | `host_rejected` | `false` | `same_command`; `none` with `details.leftover_drafts` (a rerun is refused until a human deletes the drafts in the web UI; the message says so) |
  | `lower_contract_mismatch`, own `audit_failed` | before any write: `false`; after a write: the write's own values (exit 0: `true`, `none`) | before any write: `same_command`; after: the write's |
  | `outcome_unknown` | `null` | `same_command` |
  | `outcome_partial`, backend `gitlab` | `true` when `details.landed` has ids or thread ids, else `false` | `same_command` (the key completes the state step) |
  | `outcome_partial`, backend `github` | `false` (forgectl's dedupe hit with the state gone: no write) | `new_key` |
  | `quick_action_ran`, `approved_stale_head`, forgectl's `audit_failed` | `true` | `none` |

  `same_command`: rerunning the same command cannot duplicate text and can complete it; for `review`, when the rerun exits 20 `stale_head`, add `--reanchor-to <current head>`. `new_key`: the same key cannot complete it; post a new draft or pass `--dedupe-key`. `none`: it landed, or a human must act first. The backend is `show`'s `backend`.
- `hook` and `hook_stderr` (untrusted, at most 4096 bytes) name only pr-comment's own vetoing or escalating hook; a forgectl hook's veto has them in `lower_error`. With several escalations, `hook_stderr` is the first's.
- `escalated_by` (`pr-comment` or `forgectl`) and `escalated_hooks` (`[{hook, index}]`, from pr-comment's hooks or copied from forgectl's envelope) are set on `escalated`, null otherwise; `escalated_by` is the scope a caller passes to `--override-escalation=<scope>`.
- Own and passed-through details: an own `stale_head` always has `details.head_sha`; a passed-through one has forgectl's details.
- `details` by code: `stale_head` → `head_sha`; `stale_base` → `base_ref`; `invalid_draft` → `errors: [{pointer, message}]`; `invalid_text` → `pointer`, `kind`; `unsupported_draft_version` → `supported`; `draft_pr_mismatch` → `draft_pr`, `pr`; `tool_missing` → `install`; `lower_contract_mismatch` → `command` (the forgectl subcommand), `lower_version` (string or null); `config_invalid` → `key`; `invalid_position` → `invalid` (forgectl's items); `host_rejected` → `leftover_drafts`; `body_too_large` → forgectl's details plus `role`; `not_found` → `what` (pr, to or thread), `id`; `plan_changed` → `plan_sha256`; `audit_failed` → `outcome` (the result document).

## Hooks

Configured as `hooks.<name>`, a list of `{cmd, timeout, async, with_text}` (design guide section 8), plus `--hook <name>=<cmd>`. `timeout` defaults to 30 s and `with_text` to `false`; `with_text` is allowed on both hooks, `async` only on `post-post` (else `config_invalid`).

| Hook | When | Effect |
|---|---|---|
| `pre-post` | Once per forgectl write, after confirmation and before the write; for the run's first write, before the `attempted` audit record. | Exit 0 continue; 13 escalate (remaining hooks run; then exit 13 `escalated`, nothing sent, unless the scope `pr-comment` of `--override-escalation` counts it as allow); other non-zero veto (exit 11, later hooks skipped). A timeout is a veto. A veto or escalation before the first write is logged only. A retry of a post that already landed runs the hooks too; forgectl then posts nothing. |
| `post-post` | After each forgectl write, also when it failed. | Cannot undo; failures are logged and warned. |

- Payloads ([schemas/hooks/pre-post.json](../schemas/hooks/pre-post.json), [post-post.json](../schemas/hooks/post-post.json)) carry `tool`, `hook`, `object_type: "pr"`, `object_id` (the PR, `#N` form), `repo`, `action` (`review`, `approve`, `comment`, `reply`, `resolve`, `unresolve`), `actor`, `decision_ids`, `attempt_id`, `write_index`, `plan_sha256`, `override_escalation`, `head_sha`, `anchor_head`, `base_ref`, `event`, `dedupe_key`, `counts` (`inline`, `moved` by reason, `file_level`, `severity` by severity, `suggestions_degraded`, `mentions_neutralized`, `links`, `images`, `raw_html`, `invisible`), `bodies` (`[{role, sha256, bytes}]`, `role` being `body` or `inline:<finding id>`), `to`, `thread_id`, `thread_author_is_self`, `thread_path`, `thread_line`. There is no `labels` key: pr-comment stores no objects.
- **Nullability.** `head_sha` is the pinned head (`comment` and `reply`: `show`'s head or `--expected-head`; `resolve` and `unresolve`: the `comments` read's head). `base_ref` is the pinned base (`comment` and `reply`: `show`'s base), null for `resolve` and `unresolve`. `event` only for `review` and `approve`; `dedupe_key` null only for `resolve` and `unresolve`; `to` only for `reply`; `thread_id`, `thread_author_is_self`, `thread_path` and `thread_line` only for `resolve` and `unresolve`; `bodies` empty only for `resolve` and `unresolve`. `plan_sha256` is null for the `resolve` and `unresolve` commands and set on a posting command's follow-up resolve. `write_index` is the write's position in the run (the audit outcome's `writes[].index`). `override_escalation` is the list of scopes given (`[]` when none). `anchor_head` is `anchor.head_sha` for `review`, null otherwise. `counts` is all zeros for `resolve`, `unresolve` and a follow-up resolve (every `moved` and `severity` key 0).
- `with_text: true` adds each body's final text to its `bodies` entry (`text`, untrusted). Hook stdin is an allowed transport for untrusted text.
- `post-post` adds `result`: `{exit, code, reason, ids, thread_ids, review_id, already_posted, neutralized_lines, head_verified, changed, warnings}`; `code` and `reason` null on exit 0; `ids` and `thread_ids` `[]` on failure; `already_posted` and `neutralized_lines` null on failure; `changed` only for `resolve` and `unresolve`.
- **Stdout:** at most 64 KiB; a JSON object with `decision_id` (a string or list, each matching the decision-id grammar) is recorded in the outcome's `hook_decision_id`, passed to that write's forgectl call as `--decision-id`, and shown to later hooks in `decision_ids`; at most 32 ids in all, extras dropped with a warning; invalid output is warned and ignored.
- **Environment:** pr-comment's environment minus forgectl's ambient token list (`GH_TOKEN`, `GITHUB_TOKEN`, `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, `GITLAB_TOKEN`, `GITLAB_ACCESS_TOKEN`, `OAUTH_TOKEN`) and the names in config `hook_env_deny`, plus `PR_COMMENT_HOOK`, `PR_COMMENT_ID` (the PR) and `PR_COMMENT_OBJECT_TYPE=pr`. **Cwd:** a private empty 0700 directory under the temporary directory, removed afterwards. Hooks run via `sh -c` in their own process group, which pr-comment signals on timeout.
- forgectl's own `pre-post` and `post-post` hooks still run inside each forgectl write.

## Config

`~/.config/pr-comment/config.yaml` (respecting `XDG_CONFIG_HOME`), then `config.d/*.yaml` in lexical order. Schema: [schemas/config.json](../schemas/config.json); example: [config.example.yaml](../config.example.yaml). Invalid config exits 2 `config_invalid` naming the key.

- Drop-ins: maps merge, hook lists append, other lists replace (as forgectl). `repos.<id>` overrides merge over the global values; unset keys inherit. `repos.<id>` keys are matched case-insensitively against the canonical repo id from `show`; a key that is not a valid repo id is `config_invalid`. A `config_warning` log line is written for each file that sets `allow_auto_approve` or any `mentions.*` key, globally or under `repos.<id>`, naming the file and the key paths.
- Precedence: flags, then `repos.<id>`, then environment (scalar keys only, [Environment variables](#environment-variables)), then config, then defaults.

| Key | Default | Meaning |
|---|---|---|
| `forgectl_bin` | `forgectl` | Path or name of forgectl (binding A); see [forgectl edge](#forgectl-edge). |
| `forgectl_config` | unset | Passed to forgectl as `--config`. |
| `timeout` | `120s` | Budget per forgectl call, passed as `--timeout`. |
| `me.name` | OS username | Used for the default actor and `{user}`. |
| `footer.human` | see [Footers and marker](#footers-and-marker) | String or `false`. |
| `footer.auto` | see Footers and marker | A non-empty string, never `false`. |
| `mentions.policy` | `neutralize` | `neutralize` or `allow`. |
| `mentions.allow` | `[]` | Names that pass through. |
| `gitlab.resolve_body_thread` | `non_blocking` | `non_blocking`, `never` or `always`. |
| `allow_auto_approve` | `false` | See [Automatic approval guard](#automatic-approval-guard). |
| `hooks` | `{}` | See [Hooks](#hooks). |
| `hook_env_deny` | `[]` | Extra environment variables removed from hook processes. |
| `repos.<id>` | — | Overrides `footer`, `mentions`, `gitlab` and `allow_auto_approve` for one repo. |

## Environment variables

- `PR_COMMENT_<KEY>` sets a scalar config key (`PR_COMMENT_TIMEOUT`, `PR_COMMENT_FORGECTL_BIN`, `PR_COMMENT_FORGECTL_CONFIG`, `PR_COMMENT_ME__NAME`); lists, maps and `allow_auto_approve` cannot be set from the environment.
- Hook variables: `PR_COMMENT_HOOK`, `PR_COMMENT_ID`, `PR_COMMENT_OBJECT_TYPE`.
- pr-comment reads no credentials. It runs forgectl with its own environment, and forgectl selects the per-host credential (forgectl, section Credentials).

## forgectl edge

- Binding A: pr-comment calls `forgectl_bin` and parses the documented output. It uses `show`, `diff --json`, `files`, `compare`, `comments`, `whoami`, `review`, `comment`, `reply`, `approve`, `resolve`, `unresolve` and `--version`, and every write's `--dry-run` for the [pre-check](#pre-check). No version check is made up front; compatibility is checked field by field.
- `forgectl_bin` is a bare name or an absolute path. A bare name resolves only against absolute `PATH` entries, and forgectl runs with a `PATH` of only absolute entries.
- forgectl runs with an argv list (no shell), pr-comment's environment, and `--json`. Reads run with stdin `/dev/null`. Writes and their pre-checks get their input on stdin: `forgectl review <pr> --file=- ...`, `comment|reply|approve ... --body-file=- ...`; pr-comment writes the exact plan bytes and closes the pipe. Bodies and review inputs never travel in argv, environment or files.
- pr-comment validates every value it forwards before any read, and passes each as `--flag=value`, so no value can be read as an option: `--to` against forgectl's id forms (`gh:ic:`, `gh:rc:`, `gh:rt:`, `gh:r:` plus `[A-Za-z0-9_=-]{1,100}`, `gl:d:` plus 40 hex, `gl:n:` plus an integer), `--thread` against the thread forms, `--expected-head` and `--reanchor-to` as 40 hex (uppercase lowered), `--expected-base` and `anchor.base_ref` against the branch grammar, `--dedupe-key` and `--decision-id` (including hook-supplied ids) against their grammars, `--actor` against the actor grammar. A failure exits 2 `usage` naming the flag.
- A missing forgectl exits 12 `tool_missing` with its install command in `details.install`.
- pr-comment checks forgectl's output by the fields it uses (unknown fields ignored, so additive forgectl releases keep working). A missing or ill-typed field exits 12 `lower_contract_mismatch` with `details.command` and `details.lower_version` (the first line of `forgectl --version`, display-sanitized, at most 64 characters, or null). Nothing is retried. `forgectl --version` is read only for `details.lower_version`. Supported: forgectl 0.1.x. pr-comment relies on forgectl promising `details.invalid[].index` (forgectl's error details) and the dedupe Hit fields `already_posted`, `ids` and `review_id` on a real post.
- `anchor.base_ref` is passed as `--expected-base=<ref>`.
- **Signals.** On SIGINT or SIGTERM during a forgectl call, pr-comment forwards the signal to that exact child PID, waits for it, records its result (or `outcome_unknown` when there is none) in the audit outcome, removes its hook directory and exits 12 `outcome_unknown`; before any write it exits 128+N with no envelope.
- pr-comment never calls `gh` or `glab`.

## Audit and log

Under `~/.local/state/pr-comment/` (respecting `XDG_STATE_HOME`).

- **`audit.jsonl`** ([schemas/audit.json](../schemas/audit.json)): one record pair per command sharing an `attempt_id`. Before confirmation, pr-comment checks that `audit.jsonl` can be opened for append (no-follow, regular file, mode 0600 on creation); otherwise exit 12 `audit_unavailable`, nothing sent. `attempted` is appended after the first write's `pre-post` hooks pass and immediately before that write; if the append fails, exit 12 `audit_unavailable`, nothing sent. The outcome record is appended after the last write, or when a later write's veto, escalation or failure stops the run. If it cannot be appended, exit 12 `audit_failed` with the result document in `details.outcome` (`landed` true or null, `retry: none`). A run with no forgectl write writes no audit record; refusals, vetoes and escalations before the first write go to the log.
- **Fields.** `ts`, `tool`, `version`, `attempt_id`, `actor`, `actor_source` (`flag`, `tty`, `default`), `stdin_tty`, `decision_id` (the caller's, a list), `hook_decision_id` (outcome only: ids from hook stdout), `override` (`override_escalation` whenever a scope is given, `allow_mention`), `action`, `target` (canonical `#N`), `pr_id`, `inputs` (`event`, `draft_id`, `draft_sha256`, `anchor_head`, `reanchor_to`, `pinned_head`, `base_ref`, `dedupe_key`, `to`, `threads`, `bodies: [{role, sha256, bytes}]` of the posted text, `counts`, `plan_sha256`, `allowed_mentions`, `override_escalation` (the scopes given), `config_hash` as forgectl defines it over the merged config after defaults, without flags, `config_path`), `result`, `writes` (outcome only). Records never contain bodies or secrets.
- **Writes.** `writes` lists every write the run tried after confirmation, including vetoed or escalated ones before the first `attempted` record, in order: `{index, ts, action, thread_id, dedupe_key, result, lower}`, `index` from 0 (the hooks' `write_index`), `ts` when the write started; `lower` is `{exit, code, reason, ids, thread_ids, review_id, already_posted}`, null for a vetoed or escalated write. A run whose every write was vetoed or escalated before any forgectl call writes no audit record (log only).
- **Results.** The record `result` is one of `attempted`, `done`, `already_posted`, `partial`, `refused`, `failed`, `outcome_unknown`, `vetoed`, `escalated`. A write's result: forgectl exit 0 gives `done` (`already_posted` when it reported so); 20 `refused`; 12 `outcome_unknown` gives `outcome_unknown`; 12 `outcome_partial`, `quick_action_ran`, `approved_stale_head`, `audit_failed` give `partial`; other non-zero `failed`; a `pre-post` veto `vetoed`, an escalation `escalated`. The record's `result` is its single write's, or for several: `done` when all are `done` or `already_posted`, else `partial` when any landed, else the first non-`done` write's. A real call that reports `already_posted` gives the write result `already_posted`. A review whose main write landed and whose follow-up resolve failed or was vetoed exits 0 with `body_thread_unresolved` and has the record result `partial`.
- **Pairing with forgectl's records:** until forgectl records the dedupe key and prints its `attempt_id`, pair by the same `target` and `action`, the same `thread_id` (`reply`: the `--to` id), the same head sha, a shared decision id when present, and a forgectl `attempted` record whose `ts` lies between the write's `ts` and pr-comment's outcome record; once forgectl records the key, `dedupe_key` is a pairing key too. `bodies` hash the text pr-comment handed over, without forgectl's dedupe line, so they do not equal forgectl's `body_sha256`.
- **`log.jsonl`** ([schemas/log.json](../schemas/log.json)): `{ts, level, event, pr, attempt_id, details}`. `event`: `usage_error`, `refused`, `declined`, `vetoed`, `escalated`, `finding_moved`, `suggestion_degraded`, `precheck`, `warning`, `config_warning`, `neutralizing_mismatch`. Levels as in forgectl: `error` for exit 1, `warn` for refusals, vetoes, escalations, declined dialogs and exit 12, `info` otherwise, `debug` with `--verbose`. A log write failure warns only. No bodies.
- `export` prints either file.

## Files

| Path | Content |
|---|---|
| `~/.config/pr-comment/config.yaml`, `config.d/*.yaml` | Config. |
| `~/.local/state/pr-comment/audit.jsonl` | Audit records. |
| `~/.local/state/pr-comment/log.jsonl` | Log records. |
| `~/.local/state/pr-comment/tmp/` | Mode 0700, opened without following symlinks and checked to be a directory owned by the user with mode 0700 on every run (else exit 12 `state_unsafe`). Hook working directories only: `prc-hook-<32 random hex>`, mode 0700. A run's own hook directory is removed afterwards by a file-descriptor walk that never follows symlinks and stays inside it (`unlinkat` relative to directory descriptors). At the start of a run, `prc-hook-` directories older than one day directly in the opened directory are removed with `rmdir` only when empty; nothing else is removed. |

There is no database and no migration. Upgrades affect only the [draft version](#draft-versioning) and the schemas.

## Labels

pr-comment creates no long-lived objects and takes no labels.

## TUI

None in v1.

## Residual risks

- **Head gap on comments.** Hosts enforce no head on a comment or reply; `--expected-head` is checked by a read just before the post, and a push in between is undetected (forgectl, section Residual risks).
- **Dedupe limits.** `already_posted` means a key was seen; two simultaneous runs of the same post can both land; pending reviews and drafts are not searched; a key can be planted by anyone who can post as the user; the same text posted again on purpose needs its own key.
- **Mention neutralizing relies on one host fact:** that `＠` does not link. It holds on GitHub; GitLab is unverified until the live test, which also covers `＜` and `！` not being syntax. Other references (issue links, `#N`, commit shas) pass through.
- **Markdown that renders nothing.** A link reference definition renders nothing and a link title shows only on hover, so untrusted text can carry text a reader of the page does not see; the dialog shows the raw text, and hooks can read it with `with_text`.
- **Open blocks.** An untrusted part that leaves a fence or GitLab `>>>` quote open renders pr-comment's later sections and the marker inside it (visible).
- **Neutralized commands in text.** forgectl's rule gives every line starting with `/` a leading space, which pr-comment applies itself; inside a code block in a finding's body that changes the displayed code. The dialog shows these spaces, since the plan is the posted text. Suggestions are protected by degrading them ([Suggestions](#suggestions)).
- **Advisory human actor.** A same-user process can claim `--actor human:<user>` and bypass the automatic approval guard and use `--allow-mention` or `--override-escalation`; the audit record shows `actor_source` and `stdin_tty`.
- **Allowing pr-comment allows much more.** `--hook`, `--config` and its drop-ins, `PR_COMMENT_FORGECTL_BIN` and `PR_COMMENT_FORGECTL_CONFIG` let a caller run any command and, through forgectl's config, send a stored token anywhere. A permission profile wanting less must wrap pr-comment.
- **Re-anchoring trusts the host's diff.** A rename the host does not detect moves findings to the body; different diff algorithms can align equal lines differently (always to a line with the same content). pr-comment reads forgectl's assembled diff text; its exact header forms are a forgectl contract.

## Platform notes

Pure Python with no platform-specific calls besides TTY detection and file modes: Linux, WSL and macOS behave the same. forgectl's own platform notes apply to its calls.
