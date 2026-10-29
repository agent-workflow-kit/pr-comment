# pr-comment: design

Status: Seeded from awkit architecture v1.4; not yet designed.

## Role and layer

Layer 2 (composite). Posts reviews, replies and comments to PRs, safely and with attribution. Works fully standalone (architecture C2).

## Responsibilities (architecture §5.2)

Posts to PRs.

- Commands: `review --draft-file <path> [--approve] [--reanchor-to <sha>]`, `reply --body-file <path>|-`, `comment --body-file <path>|-`, `resolve`, `approve`.
- Uses: forgectl.
- Notes: Defines the review-draft format in its `schemas/`. `review` posts as COMMENT (or REQUEST_CHANGES when the draft says so) unless `--approve` is passed. `review`, `approve`, `reply` and `comment` are confirm-by-default with no pending store (section 12.4). Checks the PR head SHA before posting and refuses on a mismatch (exit 20, `reason: stale_head`). Owns re-anchoring: `review --reanchor-to <sha>` moves findings onto a new head, and findings that cannot be re-anchored move to the review body. Appends attribution footers containing the marker `<!-- pr-comment:v1 -->` (section 14). Writes audit records. Nothing passes `--approve` or calls `approve` automatically by default.

Section numbers above refer to `../awkit-docs/architecture.md`.

## Uses

- forgectl: binding TBD (A/B/C), decided in this design session

## Cross-cutting sections to read

In `../awkit-docs/architecture.md` (plus `../awkit-docs/design-guide.md` throughout):

- §6: conventions summary (`--actor`, `--decision-id`, untrusted text in files)
- §10.1: flow A: posting an accepted review draft
- §12.1, §12.3, §12.4: public defaults, gates (enforced allow required for automatic actions), confirmations and `--yes`
- §13.2: prompt injection: transport rule 2 (bodies and drafts only via files or stdin)
- §14: audit records and attribution footers with the `<!-- pr-comment:v1 -->` marker

## Constraints (decided)

From architecture §20: none.

## Open topics

From architecture §20:

- review-draft file format
- re-anchoring algorithm
- footer templates

## Decisions

| Decision | Rationale | Date |
|---|---|---|

## Questions for the design session

1. What is the review-draft file format (body, inline findings, event COMMENT/REQUEST_CHANGES, anchors, metadata), and how is it versioned?
2. How does re-anchoring map findings onto a new head, and what moves to the review body when it cannot?
3. What footer templates ship by default, and how are they configured (always including `<!-- pr-comment:v1 -->`)?
