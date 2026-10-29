# pr-comment

Posts reviews, replies and comments to PRs, safely and with attribution.

## Status

Pre-design: interface not yet specified; see [docs/design.md](docs/design.md).

## Layer

L2 (composite). Works fully standalone; it does not need the `awkit` harness.

## What it does

- Defines the review-draft format in its `schemas/`.
- `review` posts as COMMENT (or REQUEST_CHANGES when the draft says so) unless `--approve` is passed.
- `review`, `approve`, `reply` and `comment` are confirm-by-default with no pending store.
- Checks the PR head SHA before posting and refuses on a mismatch (exit 20, `reason: stale_head`).
- Owns re-anchoring (`review --reanchor-to <sha>`).
- Appends attribution footers containing the marker `<!-- pr-comment:v1 -->` and writes audit records. Nothing approves automatically by default.

## Indicative commands

Indicative, finalized in [docs/interface.md](docs/interface.md).

- `pr-comment review --draft-file <path> [--approve] [--reanchor-to <sha>]`
- `pr-comment reply --body-file <path>|-`
- `pr-comment comment --body-file <path>|-`
- `pr-comment resolve`
- `pr-comment approve`

## Dependencies

- forgectl: binding: TBD (A/B/C), decided in this tool's design session

## Install

```sh
uv tool install git+https://github.com/agent-workflow-kit/pr-comment
```

## Documentation

- [awkit architecture](../awkit-docs/architecture.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/architecture.md))
- [awkit design guide](../awkit-docs/design-guide.md) ([published](https://github.com/agent-workflow-kit/awkit-docs/blob/main/design-guide.md))

## License

MIT — see LICENSE.
