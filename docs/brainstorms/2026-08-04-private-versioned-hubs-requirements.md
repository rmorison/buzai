---
date: 2026-08-04
topic: private-versioned-hubs
---

# Private, Versioned, Reviewable Hubs — Requirements

## Problem Frame

Hubs are the assistant's declared source of truth — `docs/decisions/knowledge-management.md`
establishes "hub wins when a hub and a brief disagree." Yet in the shipped template hubs are
**pure convention**: a gitignored directory holding a README and one scaffold, with zero code
references anywhere in `scripts/`, `trust/`, `deploy/`, or the `Makefile`. Nothing reads a hub
path, nothing backs hubs up, nothing versions them.

Two consequences follow, and both are live on the running instance today:

- **No durability.** `docs/SETUP.md` warns the adopter to copy `hubs/` out before re-cloning.
  That warning is the entire story. If the host dies, the assistant's accumulated knowledge
  dies with it. (`docs/decisions/knowledge-management.md` says a read-only Drive mirror was
  "preserved from the PoC" — verified absent from this template. The decision has drifted from
  the code.)
- **No accountability.** An always-on agent edits this store autonomously. There is no way to
  see what it wrote, judge whether it was right, or undo it. The audit log records that a gate
  decision happened; nothing records what the assistant *said* as a result.

The PoC already tried the obvious fix and it failed instructively. Its read-only Google Drive
mirror provided no versioning and no way to review or correct — hubs were opaque unless the
owner force-synced to a private GitHub repo and reviewed there. A viewing surface that doesn't
support what the owner actually does becomes dead weight.

The owner's framing is "my knowledge, my wikipedia" — review it, correct it, extend it. For
**this pass** the priority is **validation, not authoring**: look at what's in the hubs, approve
or reject, and let the assistant perform the copy-edit. Browsable rendering is desirable but
deferred until real hub content reveals whether the owner navigates or interrogates.

One constraint shapes every decision below. The app runs from a clone of a **public** repo, and
`trust/config/connector-legs.toml` declares Bash a blind spot in its own words: *"the gate does
NOT meaningfully constrain Bash... Real containment is STRUCTURAL... Do NOT rely on the gate to
stop Bash-based exfiltration."* Git runs through Bash. Therefore **hub privacy cannot be a gate
rule — it must be structural.**

Nor can it be a pre-commit rule. The repo has a real leak gate — `.pre-commit-config.yaml` runs
gitleaks plus `scripts/publish_gate.py` against a private PII/infra pattern file, with CI as
backstop — but those hooks are installed by `make dev`, a developer target. **Verified on the
running instance: no pre-commit hook exists there.** The gate protects the maintainer's
workstation; the machine where an autonomous agent holds ungated Bash has no gate at all.

Hubs are the first instance of a general problem, so the privacy requirements below are stated
for **personal state** rather than for hubs specifically. Three tiers fall out, and they have
genuinely different needs:

- **Public, tracked** — machinery, scaffolds, curated baseline configuration.
- **Private, versioned** — hubs today; history is the point.
- **Private, never versioned** — secrets and credentials. This is the *opposite* requirement
  from hubs: a secret committed once persists in history after deletion, and in a public repo
  that is world-mirrored and cannot be un-published. Secrets therefore belong at a path with no
  git repository near them, not merely a gitignored one.

```
  PUBLIC  github.com/rmorison/buzai          PRIVATE  hub remote
  ~/buzai/                                   ~/hubs/
    hubs/README.md      (tracked scaffold)     finance-and-tax.md, home.md, ...
    hubs/_example-hub.md (tracked scaffold)    .git ──push──▶ private remote (off-box history)
    ── no hub content, structurally ──

            assistant ──writes──▶ hub file ──commits with reviewable message──▶ history
                 ▲                                                                │
                 │                                          "what changed this week?"
            corrects on reject                                                    ▼
                 └──────────────── owner, in the Claude apps ◀── plain-language summary
```

---

## Actors

- A1. **Owner** — the single principal. Reviews what the assistant recorded, approves or
  rejects individual changes, and directs corrections. Does *not* want to hand-edit markdown
  as part of this loop.
- A2. **Assistant** — the always-on instance. The primary author of hub content; commits its
  own changes with reviewable messages; performs corrections on owner instruction.
- A3. **Private hub remote** — off-box store holding hub content and full history. Credential
  scoping and provider trust decisions attach here.

---

## Key Flows

- F1. **Assistant records knowledge**
  - **Trigger:** The assistant learns something durable during a turn (from a connector, a
    brief, or the owner directly).
  - **Actors:** A2, A3
  - **Steps:** Writes to the relevant hub file → commits with a message stating what changed
    and why → pushes to the private remote. A push failure does not fail the write.
  - **Outcome:** The knowledge is durable locally and, once reachable, off-box.
  - **Covered by:** R5, R6, R7, R14

- F2. **Owner reviews and corrects**
  - **Trigger:** Owner asks what changed, or receives a digest.
  - **Actors:** A1, A2
  - **Steps:** Owner asks from the Claude desktop or mobile app → assistant summarizes recent
    hub changes in plain language, not raw diffs → owner approves or rejects individual items
    → assistant corrects rejected content → the correction is committed.
  - **Outcome:** Hub content reflects owner judgment, and the review outcome is itself in history.
  - **Covered by:** R9, R10, R11, R12

- F3. **Recover onto a fresh instance**
  - **Trigger:** Host loss, migration, or a second instance.
  - **Actors:** A1, A3
  - **Steps:** Provision the instance → restore hubs from the private remote in one documented
    step → assistant resumes against full content and history.
  - **Outcome:** At most the most recent unpushed change is lost.
  - **Covered by:** R7, R8

---

## Requirements

**Hub store and layout**

- R1. Real hub content lives in a private git repository **outside** the buzai checkout. The
  public repo continues to ship only the tracked scaffolds (`hubs/README.md`,
  `hubs/_example-hub.md`).
- R2. The assistant locates hub content through a documented convention rather than an assumed
  path, so the location is changeable without editing code.

**Personal-state privacy — applies to every private artifact class, not only hubs**

- R3. Personal state must be **structurally incapable** of reaching the public repo. Protection
  may not depend on the trust gate (Bash is a declared blind spot), on pre-commit hooks (absent
  on deployments), or on `.gitignore` alone. Each of those is a convention or a
  developer-workstation control; the barrier must hold on the deployed instance without them.
- R4. Any credential able to write to a private remote is scoped to that remote only, so
  worst-case misuse writes the owner's data to the owner's own private store.
- R15. Secrets and credentials live at a path with **no git repository anywhere above them** —
  neither the public checkout nor the private state repo — and are never versioned.
- R16. The public repo continues to track only `.example` scaffolds for secret-bearing config
  (`deploy/env/*.env.example`); real values exist only at the documented outside-the-repo path.
- R17. The current inconsistency is consolidated: real secret values under `deploy/env/*.env`
  (inside the checkout, gitignore-protected only) move to the documented outside-the-repo
  location alongside `~/.config/buzai/secrets/`, leaving one rule instead of two.

**Versioning and durability**

- R5. Every assistant-made change to hub content is committed with a message stating what
  changed and why, written so the owner can review it *without* reading the diff.
- R6. Commit attribution distinguishes assistant-authored changes from owner-authored ones.
- R7. History is pushed off-box automatically; a total loss of the instance loses at most the
  most recent unpushed change.
- R8. Restoring hub content and full history onto a fresh instance is a documented single-step
  operation.
- R14. An unreachable remote must not block the assistant from recording knowledge. The write
  succeeds locally and the push is retried.

**Review loop**

- R9. The owner can ask what changed in hubs over a period and receive a plain-language
  description of each change, not raw diffs.
- R10. The owner can approve or reject individual changes conversationally; a rejection results
  in the assistant correcting the hub content.
- R11. A rejected change and its correction are both preserved in history, so review outcomes
  are auditable rather than silently overwritten.
- R12. The review loop works from the Claude desktop and mobile apps with no additional surface
  to install, host, or log into.

**Trust posture**

- R13. This work introduces **no inbound write path** into hub content. Hubs remain
  outbound-only, so their current trust classification (`private_read`, no untrusted-ingest
  leg) stays correct and no gate reclassification is required.

---

## Acceptance Examples

- AE1. **Covers R3.** Given the hub entry is removed from `.gitignore` and `git add -A` then
  `git push` runs inside the buzai checkout, when the push completes, the public repo contains
  no hub content.
- AE2. **Covers R9, R10, R11.** Given the assistant added three facts to a hub this week, when
  the owner asks what changed from the mobile app, they receive three plain-language items and
  reject one; the rejected content is corrected, and both the original and the correction remain
  in history.
- AE3. **Covers R7, R14.** Given the private remote is unreachable, when the assistant records a
  new fact, the fact is committed locally, the hub write reports success, and the change reaches
  the remote once it returns.
- AE4. **Covers R8.** Given total loss of the host, when a new instance is provisioned, hub
  content and full history are restored in one documented step.
- AE5. **Covers R15, R16, R17.** Given a connector is configured with a real secret value, when
  the entire buzai checkout is inspected — tracked files and untracked alike — the secret is
  absent from it; only the `.example` scaffold is present, and the real value exists solely at
  the documented path outside any repository.

---

## Success Criteria

- The owner can answer "what has the assistant put in my knowledge base, and is it right?" in
  about a minute, from a phone, without SSH.
- Losing the server costs at most one unpushed change.
- A rejected assistant edit leaves a trail — the store shows what was written, that it was
  rejected, and what replaced it.
- `ce-plan` can proceed without inventing the layout, the privacy control, the commit
  discipline, or the shape of the review interaction.

---

## Scope Boundaries

- **No browsable rendered site or wiki in this pass.** Deferred pending evidence of how the
  owner actually browses accumulated hub content. Explicitly revisitable — it was the owner's
  stated priority and is deferred on timing, not merit.
- **No direct human editing surface** (Obsidian vault, web editor, couch authoring). Corrections
  route through the assistant.
- **No third-party sync product**, and no inbound write path into hubs (see R13).
- **No encrypting hub content into the public repo** (git-crypt, age, transcrypt). Rejected on
  the merits: ciphertext in a public repo is permanent and world-mirrored, so a later key
  compromise is retroactive and un-deletable, and encrypted blobs destroy the reviewable diffs
  that are the point of this work.
- **Personal skills, agents, and commands are a named follow-on brainstorm, not this scope.**
  The gap is verified and real: nothing under `.claude/` is tracked, and `.gitignore` covers
  only `settings.json` and `settings.local.json`, so a personal skill written on the instance
  today is untracked-and-unignored — the same bug class as the `.claude/settings.json` leak,
  but carrying executable instructions that encode workflow, connector names, and internal
  paths. It is deferred rather than folded in because Claude Code loads these from fixed paths
  under `.claude/`, so relocating them requires symlinks or path configuration — a genuine
  design question, unlike hubs, which have zero code references. R3 and R4 already cover the
  artifact class; only its layout is unresolved.
- **Versioning `trust/config/*.local.toml` is out of scope.** It is personal policy currently
  protected by gitignore alone and is a plausible member of the private-versioned tier, but it
  is not required for this pass.
- **No adopter-facing packaging.** Generalizing to the shipped template is a later decision
  informed by living with this. Choices should avoid foreclosing it.
- **No change to hub shape or precedence.** Earn-the-split and hub-wins from
  `docs/decisions/knowledge-management.md` stand unchanged.
- **No semantic or vector retrieval.** Unchanged from the existing decision.

---

## Key Decisions

- **Substrate decided independently of surface.** Git-backed versioned hubs are a prerequisite
  for every candidate review surface, cost nothing, run headless, and generalize cleanly. There
  is no version of this where the substrate is regretted, so it proceeds regardless.
- **Privacy is structural, not gate-enforced.** Follows directly from the existing Bash
  blind-spot decision in `trust/config/connector-legs.toml` rather than being a new stance. The
  pre-commit leak gate does not change this: it is a workstation control that is verifiably
  absent from deployments.
- **The privacy control is stated once, for personal state generally.** Hubs, secrets, and the
  deferred skills/agents/commands class all need the same barrier; specifying it per-artifact
  would mean re-deriving it each time, which is how `.claude/settings.json` was missed.
- **Secrets are never versioned — the opposite of hubs.** History is the value for hubs and the
  liability for secrets, since a committed secret survives its own deletion and, in a public
  repo, is world-mirrored. This is why they get a no-repo path rather than a private repo.
- **Hubs move outside the checkout.** The tracked scaffolds make an in-place symlink shadow
  tracked files, and personal knowledge should not live inside a clone of a public repo. Since
  hubs have zero code references, relocation is a documentation and convention change.
- **Review happens in the chat.** The correction path already lives there, so approve/reject and
  fix close in one place. The PoC's Drive mirror is the cautionary precedent: a viewing surface
  that went unused because it didn't support what the owner actually did.
- **Outbound-only by design.** Declining an inbound write path is what keeps hub trust
  classification unchanged (R13) and is affordable precisely because authoring is out of scope
  this pass.
- **A provider-readable private remote is accepted.** A GitHub private repo is not end-to-end
  encrypted. Accepted for this content; swapping to a self-hosted or encrypted remote later
  changes nothing else in this design.

---

## Dependencies / Assumptions

- **Verified:** hubs have no code references anywhere in the repo, so relocating them is a
  convention change rather than a refactor.
- **Verified:** the running instance's public origin has no credential helper and no stored
  credentials — a push to the public repo currently fails outright. This design assumes that
  stays true.
- **Verified:** `docs/decisions/knowledge-management.md` claims a Drive mirror is "preserved,"
  and no implementation exists. That document needs updating when this lands.
- **Verified:** the live instance has only scaffolds in `hubs/`, so there is no migration burden.
- **Verified:** the pre-commit leak gate (gitleaks plus `scripts/publish_gate.py`) is installed
  by `make dev` and is **not present on the running instance** — checked directly on the host.
  Deployment-side protection must be structural rather than hook-based.
- **Verified:** nothing under `.claude/` is tracked, and only `settings.json` and
  `settings.local.json` are gitignored, so the skills/agents/commands class is currently
  unprotected. Recorded here because R3 covers it even though its layout is deferred.
- **Verified:** two secret locations exist today with unequal protection —
  `~/.config/buzai/secrets/*.env` outside the checkout (`docs/SETUP.md:231`) and
  `deploy/env/*.env` inside it, protected by `.gitignore` alone.
- **Assumption (unvalidated):** hub change volume stays low enough that per-change commits and
  per-period review summaries remain readable rather than noisy. Hubs are currently empty, so
  this cannot be tested yet; revisit once real content accumulates.
- **Assumption:** the assistant retains Bash access to run git. This is also precisely why R3
  must be structural.

---

## Outstanding Questions

### Resolve Before Planning

_None._

### Deferred to Planning

- [Affects R5][Technical] Commit granularity and timing — per hub write, per assistant turn, or
  debounced — and how messages are composed to be reviewable without the diff.
- [Affects R9][Technical] How the assistant determines "what changed since last review" — a
  last-reviewed marker versus a time window.
- [Affects R3][Technical] Which structural barrier to implement beyond the separate repo — a
  nested-repo boundary, a pre-push guard, or both.
- [Affects R12][Technical] Whether review is pull-only ("what changed?") or can also be pushed
  on a schedule as a digest.
- [Affects R7][Needs research] Push retry and backoff behavior, and whether unpushed-change
  state is surfaced to the owner.
- [Affects R2][Technical] How the assistant is told where hubs live — project instructions,
  environment, or a config file.
- [Affects R15, R17][Technical] How the service and the assistant read secrets from the
  outside-the-repo path — sourced by the systemd unit, read on demand, or another mechanism —
  and what the migration step looks like for values already under `deploy/env/`.
- [Affects R3][Technical] Whether any deployment-side backstop is worth adding given hooks are
  absent there (for example a guard in the update path), or whether the structural separation
  alone is judged sufficient.

---

## Next Steps

-> `/ce-plan` for structured implementation planning.
