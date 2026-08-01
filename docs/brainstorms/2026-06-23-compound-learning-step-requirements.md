---
date: 2026-06-23
topic: compound-learning-step
---

# Compound Learning Step — Requirements

## Summary

An on-demand "compound" step the owner invokes after a notable piece of work: the assistant drafts learnings from the recent session, the owner refines them, and they are filed as durable notes future sessions read as grounding. Learnings split into a generalizable **machinery** store that ships with the template and a **personal** store kept with the owner's private state. Notes only — nothing modifies assistant behavior automatically. A sibling to the trust/capability model, decoupled from it.

## Problem Frame

The `buzai` PoC produces a stream of work — briefs, fixes, decisions, dead-ends — but nothing captures "what can I learn from that thing we did" in a durable way. Lessons live in the owner's head or scatter across sessions, so the assistant repeats mistakes and the owner re-derives the same conclusions. Every's `ce-compound` solves the engineering version of this by documenting solved problems where future runs read them; this brings a simplified version to a personal chief-of-staff's life-ops work. Because the product ships as a self-host template, the capture also has to respect a line the engineering version doesn't face: the owner's personal learnings must never end up in a shareable repo.

## Key Decisions

- **Durable notes, not behavior mutation.** Learnings are filed for future sessions to read as grounding; nothing self-edits the assistant. This deliberately keeps the feature clear of the trust model's always-gate (self-editing config is high-blast-radius).
- **Two stores, and the split is a privacy boundary.** A generalizable machinery store (assistant workflow, conventions, what worked) ships with the template; a personal store (lessons tied to the owner's life/domains) stays with private state and is never shipped. Misrouting a personal learning into the machinery store leaks it into a shareable repo, so the split is enforced, not cosmetic.
- **Assistant drafts, owner refines.** The assistant proposes learnings from the recent session; the owner's refine pass is also the routing and privacy checkpoint.
- **Bespoke step, `ce-compound` entry format.** Reuse Every's `ce-compound` entry/frontmatter convention so future sessions retrieve learnings the same proven way, but build a bespoke step rather than invoking `ce-compound` itself — the domain (life-ops, not engineering) and the split-store shape differ.
- **On-demand only.** The owner triggers each compound; scheduled or automatic reflection is deferred.

## Requirements

**Invocation and drafting**

- R1. The owner invokes the compound step on demand after a notable piece of work; it never runs scheduled or automatically.
- R2. The assistant drafts the learnings from the recent session and work already in its context, and the owner refines them before they are filed.

**Learning stores and routing**

- R3. Each learning is filed into one of two stores: a generalizable machinery store (assistant workflow, conventions, what worked or failed) or a personal store (lessons tied to the owner's life, projects, or domains).
- R4. The machinery store ships with the template, seeded and generalizable; the personal store lives with the owner's private state, is gitignored, and is never shipped.
- R5. The assistant proposes a store for each drafted learning; the owner's refine pass confirms or corrects the routing before anything is filed.

**Privacy and containment**

- R6. Machinery-store entries are scrubbed of personal specifics — they capture the generalizable lesson, not the owner's data.
- R7. Drafting reads private session and work content under the owner's authority; filed output must not push sensitive content past its store's audience (the personal store stays private; the machinery store is shareable and scrubbed).

**Retrieval and grounding**

- R8. Learnings are filed as per-topic entries with frontmatter (reusing the `ce-compound` entry shape) so future sessions can find and read the relevant ones.
- R9. Future sessions read relevant learnings as grounding; learnings never change assistant behavior automatically.

## Acceptance Examples

- AE1. **Covers R3, R5, R6.** Given a session that mixed a reusable workflow insight with a detail about the owner's finances, When the owner compounds it, Then the assistant proposes the workflow insight for the machinery store scrubbed of the financial detail, proposes the finance-specific lesson for the personal store, and files neither until the owner confirms the routing.
- AE2. **Covers R1.** Given a session ends with no explicit invocation, When nothing is triggered, Then no compound runs and nothing is filed.
- AE3. **Covers R8, R9.** Given a filed machinery learning about how to structure a brief, When a future session does related work, Then it can retrieve and read that learning as grounding without any behavior changing on its own.

## Scope Boundaries

**Deferred for later**

- Scheduled or automatic compounding (e.g., end-of-day reflection).
- Auto-applied learnings that edit the assistant's config, skills, or trust policy.
- Building on the audit log as a primary input — it is an optional source at most.
- Multi-principal / shared learning stores.

## Dependencies / Assumptions

- Sibling to the trust/capability model (`docs/brainstorms/2026-06-23-trust-capability-model-requirements.md`); the two are decoupled and neither blocks the other.
- Assumes the assistant has access to its own recent session and work content at invocation time (it runs in-session, owner-invoked).
- Reuses Every's `ce-compound` entry/frontmatter convention; assumes that shape (or a close variant) suits the machinery store's retrieval.
- The "future sessions read learnings as grounding" mechanism depends on how the assistant loads context — specified in planning.

## Outstanding Questions

**Deferred to planning**

- Exact store locations, file format, and frontmatter fields for each store.
- How future sessions load machinery learnings as grounding (session-start read, on-demand search, or other).
- The scrub ruleset for machinery entries — what counts as a personal specific.
- Whether the personal store overlaps the existing personal "hubs" state or is a separate store.

## Sources / Research

- `docs/brainstorms/2026-06-23-trust-capability-model-requirements.md` — sibling MVP feature; decoupled.
- the earlier private PoC's `CLAUDE.md` — the stated intent to keep generalizable machinery separable from personal state, toward an open-source plugin + chief-of-staff methodology.
- Every's `ce-compound` (compound-engineering) — documents solved problems to `docs/solutions/` and `CONCEPTS.md`; the pattern this simplifies for life-ops work.
