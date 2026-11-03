# pr-comment

Posts reviews, replies and comments to pull and merge requests, safely and with attribution.

## Status

Designed (interface 0.1.0-design); not yet implemented. The normative specification is [docs/interface.md](docs/interface.md); decisions are in [docs/design.md](docs/design.md) and the staged plan in [docs/plan.md](docs/plan.md).

## Layer

L2 (composite). Works fully standalone; it does not need the `awkit` harness.

## What it does

- Posts one review from a JSON [review draft](docs/interface.md#review-draft): inline findings, file-level findings, suggestions and a summary, as a comment or a request for changes, or as an approval with `--approve`.
- Re-anchors a draft onto a new head with `--reanchor-to <sha>`, exactly or not at all; findings it cannot place move to the review body with a reason and stay in the re-anchored draft ([Re-anchoring](docs/interface.md#re-anchoring)).
- Posts comments and replies, approves, and resolves or unresolves threads.
- Checks every post with forgectl's own dry run before asking ([Pre-check](docs/interface.md#pre-check)), then shows the exact text it will post and asks once; `--dry-run` prints that plan and its hash, which `--expect-plan` binds to the real post. Without a terminal it posts only with `--yes`, which automation passes only after a human or an enforced gate accepted the action.
- Replaces @ with ＠ in posted text unless the name is allowed (applicable suggestions excepted; [Mentions](docs/interface.md#mentions)), neutralizes `<` and `![` in untrusted text by default, so no raw HTML or images are posted ([Body structure](docs/interface.md#body-structure)), puts an attribution line at the top and the marker `<!-- pr-comment:v1 -->` at the end ([Footers and marker](docs/interface.md#footers-and-marker), [Marker detection](docs/interface.md#marker-detection)), and never approves automatically unless configured.
- Posts only through forgectl, which pins heads, neutralizes commands in posted text and audits every write; pr-comment keeps its own audit log. It holds no credentials and no database.

## Quick start

```sh
pr-comment draft new <pr> > d.json
# edit d.json: body, findings
pr-comment draft check d.json
pr-comment review <pr> --draft-file d.json --dry-run
pr-comment review <pr> --draft-file d.json
```

## Commands

| Command | Purpose |
|---|---|
| `pr-comment review <pr> --draft-file <path>\|- [--approve] [--reanchor-to <sha>] [--expected-head <sha>] [--dedupe-key <k>] [--allow-mention <name>]...` | Post a review from a draft (`--expected-head` is required with `--approve` for an `auto:` actor). |
| `pr-comment comment <pr> --body-file <path>\|- [--expected-head <sha>] [--dedupe-key <k>] [--allow-mention <name>]...` | Post a general comment. |
| `pr-comment reply <pr> --to <id> --body-file <path>\|- [--expected-head <sha>] [--dedupe-key <k>] [--allow-mention <name>]...` | Reply to a comment, thread or review. |
| `pr-comment approve <pr> [--body-file <path>\|-] [--expected-head <sha>] [--expected-base <ref>] [--dedupe-key <k>] [--allow-mention <name>]...` | Approve at a pinned head and base (both required unless interactive). |
| `pr-comment resolve\|unresolve <pr> --thread <id>...` | Resolve or unresolve threads. |
| `pr-comment draft check <path>\|-` | Validate a draft offline. |
| `pr-comment draft new <pr> [--event comment\|request_changes] [--from <path>\|-]` | Print a new draft anchored at the PR's current head. |
| `pr-comment export [--since <time>] [--log]` | Print audit or log records. |

Global flags: `--json`, `--config`, `--dry-run`, `--yes`, `--expect-plan`, `--override-escalation=<scope>`, `--actor`, `--decision-id`, `--hook`, `--verbose`, `--quiet`, `--version` ([Global flags](docs/interface.md#global-flags)).

## Exit codes

0 success (including "already posted" and dry runs), 1 internal error, 2 usage or invalid draft, 10 not found, 11 vetoed, 12 failed, 13 needs a human (confirmation or escalation), 20 refused with a `reason`. See [Exit codes](docs/interface.md#exit-codes).

## Config, hooks and environment

- Config: `~/.config/pr-comment/config.yaml` plus `config.d/*.yaml`; every key is documented in [config.example.yaml](config.example.yaml) and [Config](docs/interface.md#config).
- Hooks: `pre-post` and `post-post`, run in a private empty directory without token variables ([Hooks](docs/interface.md#hooks)).
- Environment: `PR_COMMENT_<KEY>` for scalar config keys; pr-comment reads no credentials ([Environment variables](docs/interface.md#environment-variables)).

## Dependencies

| Dependency | Binding | Install |
|---|---|---|
| forgectl | A (hard) | `uv tool install git+https://github.com/agent-workflow-kit/forgectl` |

forgectl needs `gh` (GitHub) and/or `glab` (GitLab) and their logins or tokens; see forgectl's README. A missing forgectl exits 12 with this install command.

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/pr-comment
```

## Documentation

- [docs/interface.md](docs/interface.md): normative interface
- [docs/design.md](docs/design.md): decisions and internals
- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
