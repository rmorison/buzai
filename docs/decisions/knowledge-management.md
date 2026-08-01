# Knowledge Management — Substrate and Hub Shape

- **Status:** Accepted
- **Date:** 2026-06-23
- **Related:** `docs/brainstorms/2026-06-23-trust-capability-model-requirements.md`, `docs/brainstorms/2026-06-23-compound-learning-step-requirements.md`

## Context

The assistant keeps a long-lived personal knowledge base — "hubs" — alongside the other durable stores the MVP brainstorms introduced: the standing trust policy, the audit log, and the compound learning stores (machinery + personal). Three constraints bound the choice: the brief requires documents be "viewable via a repo / file share"; the product ships as a self-host template, so infra a self-hoster must stand up is a cost; and the KB must "build up over time as life goes on." The `buzai` PoC implemented hubs as one canonical `<category>.md` per domain (9 hubs), local-canonical with a read-only Google Drive mirror, under a "hub wins when a hub and a brief disagree" precedence.

## Decision

1. **Substrate is markdown-in-git** — the same substrate every other durable store already uses. One mental model and one retrieval path (native file/content search + conventions) across the whole assistant; no separate KM engine beside it.
2. **Hub shape is earn-the-split.** A hub stays a single `<category>.md` while it is small. It becomes a directory — an index/overview file plus per-topic entry files with light frontmatter (date, tags, status) and `[[wikilink]]` cross-references — once it has grown large enough that reading it whole is wasteful or its retrieval is too coarse. Migration is a trivial markdown reshape because the substrate does not change.
3. **Retrieval is search + index, not embeddings.** Content/file search over the files, aided by a hub index where one exists. The same files open in Obsidian (markdown + wikilinks) for a human GUI.
4. **Preserved from the PoC:** hub-wins precedence, the Drive mirror, and repo/file-share legibility.

## Consequences

- **Plus:** no added infra for self-hosters; everything diffable, legible, and agent-readable; consistent with the trust policy, audit log, and compound stores; scales without a rewrite because a split is a reshape, not a migration.
- **Minus:** a large hub eventually needs a split the system won't perform for you in MVP (see Non-MVP); a directory hub adds files, a naming convention, and index upkeep.

## Non-MVP / Future

- **Hub-split advisor:** the assistant detects when a hub has grown enough to warrant breaking into a folder and recommends the split — analogous to the span-of-control project-graduation idea from ideation. MVP relies on the owner noticing; the advisor is a later convenience.
- **Semantic / vector retrieval:** add only if search + index measurably degrades at scale. Not on spec, not foreclosed.

## Alternatives considered (rejected for MVP)

- **One file per domain forever:** simplest, but a multi-year KB becomes a single huge file that is costly to read whole and coarse to retrieve.
- **Vector/RAG, knowledge graph, or MCP memory server:** stronger semantic retrieval at scale, but each adds infra a self-hoster must run, introduces sync/staleness, is less legible, and breaks "viewable via a repo." Prior ideation rejected the typed-graph for the same reasons. Deferred, not foreclosed.
