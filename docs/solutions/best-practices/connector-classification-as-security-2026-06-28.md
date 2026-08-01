---
title: Connector classification as a security boundary
date: 2026-06-28
category: best-practices
module: trust gate / connector preset catalog
problem_type: best_practice
component: assistant
severity: high
related_components:
  - tooling
  - documentation
  - development_workflow
applies_when:
  - Assigning trifecta leg-state to a new or existing connector
  - Maintaining the active-vs-commented connector catalog
  - Reviewing whether a connector can read untrusted content or exfiltrate
tags:
  - connectors
  - trust-boundary
  - lethal-trifecta
  - classification
  - catalog
  - security
---

# Connector classification as a security boundary

## Context

A trifecta gate can only refuse the lethal combination if it knows which legs each tool touches. That knowledge lives in a **connector preset catalog** — a map from tool-name patterns to the trifecta legs (`private_read`, `untrusted_content`, `external_send`) they carry. This catalog is not documentation or a convenience; it is the gate's threat model encoded as data. A single mislabel — a send tool tagged read-only — is an un-gated exfiltration path that the rest of the system will faithfully honor.

## Guidance

**Apply a conservative classification rubric:**

- **Any external/shared READ = `private_read` + `ingests_untrusted`.** Issues, messages, docs, calendar invites, shared-project tasks, and comments all carry text authored by other people — a known prompt-injection vector. Treat inbound content as untrusted by default.
- **Any visible MUTATION = `external_send`.** Creating, updating, posting, commenting, assigning, sharing, or moving into shared state reaches other people or external systems. That is a send leg.
- **Destructive / irreversible / money-moving ops = hard-gated (always-gate).** These ASK regardless of tier or any promotion. An irreversible op can never be promoted away.
- **Only the owner's own private state reads as `private_read` alone** — with no untrusted leg.

**The active/commented split is the trust boundary.** Ship only the presets you can name-verify as ACTIVE against a real instance. Ship everything else COMMENTED, with a "validate the real tool names via `/mcp` before enabling" note. An unused preset must never be a silent trust grant: a commented block grants nothing, and forces the operator to confirm the actual tool names before the gate starts reasoning about them. (Placeholder prefixes that match nothing are worse than absence — every call falls to the unknown-tool path and the connector's real legs are never applied.)

**For a read-default connector, the send-overrides must be EXHAUSTIVE over its mutations.** When a connector's wildcard rule defaults to `private_read`, every mutation tool needs an explicit override adding `external_send`. A new or unlisted mutation tool falls through to the read wildcard and silently reads as private — an un-gated send. When you enable a connector, cross-check its full `/mcp` tool list and override *every* create/update/post/comment/assign/share/move op, not just the obvious ones.

**Review read-side tagging as hard as send-side.** A review of this catalog caught read-side *under*-tagging: calendar invites (attendee-supplied titles, descriptions, locations) and shared-project content are attacker-controllable untrusted inbound, but were initially tagged `private_read` only. Under-tagging a read is just as exploitable as mislabeling a send — it lets poisoned content in without lighting the untrusted leg.

## Why This Matters

The gate's correctness is bounded by the catalog's accuracy. Every other safety mechanism — Rule-of-Two, new-recipient gating, hard-gates — operates on the legs this catalog assigns. A convenience-minded catalog (tag things by how annoying the prompt is, not by blast radius) produces a gate that looks strict but has holes exactly where it matters. Treating the catalog as a security artifact — conservative rubric, exhaustive overrides, verified-active boundary — is what makes the gate trustworthy.

## When to Apply

- Whenever you add or enable a connector/MCP server on a gated agent.
- Whenever a connector adds new tools (vendors add tools; your overrides can silently fall behind).
- Whenever you review a trifecta/capability gate — audit the classification table first; it is the foundation everything else stands on.

## Examples

A read-default connector with the untrusted leg on reads and explicit send-overrides on every mutation (calendar shown; the more-specific rule wins over the wildcard):

```toml
# trust/config/connector-legs.toml
# Calendar reads are private AND ingest untrusted content: invite titles,
# descriptions, locations are attendee-supplied (a prompt-injection vector).
[[rule]]
match = "mcp__claude_ai_Google_Calendar__*"
legs = ["private_read"]
ingests_untrusted = true

[[rule]]
match = "mcp__claude_ai_Google_Calendar__create_event"
legs = ["external_send"]
[[rule]]
match = "mcp__claude_ai_Google_Calendar__update_event"
legs = ["external_send"]
[[rule]]
match = "mcp__claude_ai_Google_Calendar__respond_to_event"
legs = ["external_send"]
```

The standing rule, stated at the top of the catalog, that makes exhaustiveness a discipline:

```text
STANDING RULE for a connector whose read wildcard is the default (private_read): its
external_send overrides MUST be exhaustive over that connector's mutations. A mutation
tool whose exact name isn't overridden falls through to the read wildcard and carries
NO external_send — a silently un-gated send.
```

The active/commented trust boundary — a directory connector ships commented with an enable checklist, granting nothing until verified:

```toml
# Directory connectors (OPT-IN — commented out by default) so an unused preset is
# never a silent trust grant. To enable: add the connector, run `/mcp` for REAL
# names, uncomment, FIX the match patterns, add any hard-gate classes_of entry,
# then verify with the per-connector smoke check.
#
# [[rule]]
# match = "mcp__github__*"
# legs = ["private_read"]
# ingests_untrusted = true
# [[rule]]
# match = "mcp__github__create_*"   # issue/PR/comment; also update_*/add_comment
# legs = ["external_send"]
# # hard-gate: policy.toml [classes_of]  "mcp__github__merge_*" = "merge"
```

Hard-gating irreversible ops — the always-gate class list plus the `classes_of` binding force an ASK regardless of any promotion:

```toml
# trust/config/always-gate.toml
classes = ["money_movement", "delete", "share_link", "merge", "publish",
           "grant_connector_scope", "edit_trust_config", "Agent", "Task"]
```

```toml
# trust/config/policy.toml — bind a specific destructive tool to an always-gate class
[classes_of]
"mcp__claude_ai_Google_Calendar__delete_event"  = "delete"
"mcp__claude_ai_DropboxMCP__delete"             = "delete"
"mcp__claude_ai_DropboxMCP__create_shared_link" = "share_link"
```

A declared blind spot is documented rather than mislabeled — Bash is general-purpose and can do all three legs in one command, so static leg-classification cannot capture it; it is mapped to zero legs (usable, defaults to `review`/ask) with an explicit note that real containment is structural (unprivileged user, egress limits, sandboxing), not the gate.

## Related

- [Enforcing the lethal trifecta without self-bricking](../design-patterns/enforcing-lethal-trifecta-without-self-bricking-2026-06-28.md) — the gate that consumes these legs; turn-scoping and unknown-tool handling depend on `classify()` reading this catalog.
- [Verify a collaboration tool's real access model](verify-collaboration-tool-access-model-2026-06-28.md) — before trusting a *new* tool with sensitive content, verify its access semantics (the catalog assumes a tool's read/write model; verify it).
- Origin: issue #9, PR #11, `docs/plans/2026-06-27-006-feat-connector-preset-catalog-plan.md`.
- Code: `trust/config/connector-legs.toml`, `trust/config/always-gate.toml`, `trust/config/policy.toml`, `trust/classify.py`.
