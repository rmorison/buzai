---
title: "feat: Private, versioned, reviewable hubs"
type: feat
status: active
date: 2026-08-05
origin: docs/brainstorms/2026-08-04-private-versioned-hubs-requirements.md
deepened: 2026-08-05
---

# feat: Private, versioned, reviewable hubs

## Overview

Move the assistant's knowledge base ("hubs") out of the public-repo checkout into a private git
repository outside it, give every assistant-made hub change a reviewable commit pushed off-box,
and add a conversational review loop the owner drives from the Claude apps. Consolidate secrets
onto one outside-the-repo location at the same time, since they are the same partition problem
with the opposite versioning requirement.

The organizing principle: **the public repo is a template source, and every piece of personal
state is an instantiation of a template into a location outside the checkout.** Hubs and secrets
both follow it, joining the three instances that already exist
(`deploy/env/*.env.example`, `deploy/claude-remote.service.template`,
`trust/settings.example.json`). This makes `~/buzai` a system directory that `git pull` can
update without any possibility of trampling personal data.

---

## Problem Frame

Hubs are the declared source of truth — `docs/decisions/knowledge-management.md` establishes
"hub wins when a hub and a brief disagree" — yet they are pure convention: a gitignored directory
with zero code references anywhere in the repo. Nothing versions them, nothing backs them up,
and nothing lets the owner see what the always-on assistant wrote into them. Both consequences
are live on the running instance today (see origin: `docs/brainstorms/2026-08-04-private-versioned-hubs-requirements.md`).

Privacy here cannot be enforced by the usual mechanisms, and this is settled rather than
discovered: `trust/config/connector-legs.toml` declares Bash a blind spot ("Do NOT rely on the
gate to stop Bash-based exfiltration"), git runs through Bash, and the pre-commit leak gate is a
`make dev` developer target verified absent from the deployment. The barrier must be structural.

---

## Requirements Trace

**Hub store and layout**
- R1. Hub content lives in a private git repo outside the checkout; public repo ships scaffolds only.
- R2. The assistant locates hubs through a documented convention, not an assumed path.

**Personal-state privacy**
- R3. Personal state is structurally incapable of reaching the public repo — not dependent on the gate, pre-commit hooks, or `.gitignore` alone.
- R4. Credentials that can write a private remote are scoped to that remote only.
- R15. Secrets live at a path with no git repo above them and are never versioned.
- R16. The public repo tracks only `.example` scaffolds for secret-bearing config.
- R17. Real values under `deploy/env/*.env` consolidate onto the outside-the-repo location.

**Versioning and durability**
- R5. Every assistant hub change is committed with a message reviewable without the diff.
- R6. Commit attribution distinguishes assistant-authored from owner-authored changes.
- R7. History is pushed off-box automatically.
- R8. Restore onto a fresh instance is a documented single-step operation.
- R14. An unreachable remote never blocks recording knowledge.

**Review loop**
- R9. The owner can ask what changed and get plain language, not diffs.
- R10. The owner can approve/reject individual changes conversationally; rejection triggers correction.
- R11. Rejections and their corrections are both preserved in history.
- R12. The review loop works from the Claude apps with no additional surface.

**Trust posture**
- R13. No inbound write path; no *gate-visible* trust reclassification is required (see Key Decisions for the restatement and the accepted gap).

**Origin actors:** A1 (Owner), A2 (Assistant), A3 (Private hub remote)
**Origin flows:** F1 (assistant records knowledge), F2 (owner reviews and corrects), F3 (recover onto a fresh instance)
**Origin acceptance examples:** AE1 (covers R3), AE2 (covers R9, R10, R11), AE3 (covers R7, R14), AE4 (covers R8), AE5 (covers R15, R16, R17)

---

## Scope Boundaries

- No browsable rendered site or wiki — deferred on timing, not merit (origin).
- No direct human editing surface; corrections route through the assistant (origin).
- No third-party sync product and no inbound write path (origin, R13).
- No encrypting hub content into the public repo (origin — rejected on the merits).
- Personal skills, agents, and commands under `.claude/` remain a named follow-on brainstorm (origin).
- Versioning `trust/config/*.local.toml` stays out of scope (origin).
- No adopter-facing packaging decisions; choices must simply avoid foreclosing it (origin).
- No change to hub shape, hub-wins precedence, or retrieval approach (origin, `docs/decisions/knowledge-management.md`).
- **No scheduled/push digest.** Review is pull-only this pass.
- **No history rewriting and no automatic merge of remote content.** See the divergence decision below.

### Deferred to Follow-Up Work

- Template-drift reporting (public scaffolds updated after a hub repo was instantiated).
- Active notification (email/push) of a stale push backlog, beyond the surfacing this plan adds.

---

## Context & Research

### Relevant Code and Patterns

- `scripts/secrets_preflight.py` — the fail-closed `ExecStartPre` gate. Pure-core (`perm_problems`, `tracked_problems`) with injected `is_tracked`. The extension point for structural checks.
- `scripts/trust_check.py` — richest example of pure-function + injected-side-effect-callable; `main(argv=None) -> int`, tagged output, exit 0/1.
- `trust/provenance.py` — write-temp-then-`Path.replace()` atomic write. Reuse for hub content writes.
- `scripts/install_hooks.py` — tracked-example → generated-artifact instantiation with idempotency and refuse-to-clobber semantics.
- `deploy/claude-remote.service.template` — `ExecStartPre` wiring; `EnvironmentFile=` without `-` is deliberately fail-closed; sets **only** `Environment=PATH=…`, so no shell-profile variable reaches the service.
- `Makefile` — every verb needs `## ` help text; verb names are public API (`CONTRIBUTING.md`).
- Tests: `scripts/tests/test_trust_check.py` is the model — `tempfile.TemporaryDirectory`, injected callables instead of mocks, `redirect_stdout`, and the `boom` sentinel idiom.
- **`scripts/publish_gate.py` is NOT a secret detector.** Verified: `GENERIC_PATTERNS` contains exactly one entry (`SESSION-HANDOFF\.md`); real coverage comes from `~/.config/buzai/gate-patterns`, which does not exist on the deployment. It is a public-repo PII/infra matcher, and its private patterns enumerate exactly the hostnames and handles a legitimate personal hub contains.

### Institutional Learnings

- `docs/solutions/architecture-patterns/running-claude-code-always-on.md` — **prior bug to avoid:** the original `secrets_preflight.py` tracked-check was fed only paths outside the repo, making it a dead no-op with false confidence. Every new check must be fed paths that can actually fail and must have a test that plants a violation. Also §6(f): do not fail on legitimate absence.
- `docs/solutions/design-patterns/enforcing-lethal-trifecta-without-self-bricking-2026-06-28.md` — safety controls must fail loudly, never degrade into "active but silently broken."
- `docs/solutions/best-practices/connector-classification-as-security-2026-06-28.md` — Bash is deliberately zero-legs; do not attempt gate rules for `git push`.
- `docs/solutions/best-practices/verify-collaboration-tool-access-model-2026-06-28.md` — verify a provider's real access model empirically rather than trusting the label.

---

## Key Technical Decisions

- **The lock spans read → modify → write → scan → commit; the push happens outside it.** Same-dir sessions mean two attaches can update the same hub file concurrently. Serializing only commit+push does not serialize the *update* — both sessions would produce commits while one session's content is silently clobbered, and the losing commit's message would describe a fact the file no longer contains, breaking R5's "reviewable without the diff" premise. The push is excluded because git has no default network timeout: a blackholed connection would hold the lock indefinitely and block every write on the box, inverting R14. Push is idempotent and its failure is already handled by the backlog.
- **`hub_commit` takes an operation, not pre-rendered file content.** Append-entry / replace-section / remove-entry. If the caller pre-renders the whole file, the read happened outside the critical section and the race returns.
- **Privacy is verified without `gh`, by an anonymous readability probe.** An unauthenticated `git ls-remote` that *succeeds* proves the repo is publicly readable. This is provider-agnostic, needs no new dependency, and avoids a fatal contradiction: `gh auth login` installs a global `github.com` credential helper that would give the instance push access to the **public** origin, destroying the verified assumption that it has none — while a deploy key (the R4-compliant option) has no API access at all, so `gh repo view` could never succeed and fail-closed would refuse every push forever. The two halves of the original design were mutually exclusive.
- **Verification is cached with a TTL, not only on URL change.** Visibility can flip to public through the web UI or an org policy change with the clone URL unchanged, so a URL-keyed cache never re-checks. Re-verify at service start and on TTL expiry; an expired cache means refuse-and-queue, never push.
- **Divergence halts; it never merges remote content into local hub files.** Rebasing would pull remote commits into files the assistant then reads back as trusted source of truth — a genuine inbound path that contradicts R13 and would let a compromised remote or credential inject "approved" content. Halt and surface conversationally instead. Force-push is never used.
- **R13 restated honestly.** Hub push *is* a new outbound flow of personal data to a third party. It raises no gate leg only because Bash is a declared blind spot — so a test asserting "no external-send leg" is tautological and proves nothing. R13 means *no new gate-visible classification change*; the outbound flow is an accepted, documented gap, justified by the destination being verified-private. Recorded as such in the ADR.
- **The hub leak scan detects credential shapes, not PII.** `publish_gate.py` is the wrong tool twice over: on the deployment its pattern file is absent so it matches almost nothing, and on the maintainer's workstation its private patterns match the very hostnames and handles a legitimate personal hub is *supposed* to contain, so it would refuse valid commits. Use entropy/regex credential detection (gitleaks-equivalent rules) and explicitly do not apply public-repo PII patterns to private hub content.
- **Rejection defaults to removal, not re-derivation.** "Semantic correction" has no defined input — the writing session is dead, the source material is gone, and the diff hunk is explicitly excluded — so an implementer would either hallucinate a replacement or make a cosmetic edit and mark it corrected. A rejection carries the owner's reason and *optionally* a correct value; with no value supplied, the rejected content is removed. Removal is well-defined regardless of how much the file has changed since, which is what the stale-rejection cases actually need.
- **Review state lives in git notes, not a JSON file.** A shared mutable JSON file is defective either way: uncommitted, the R11 audit trail dies with the host and R8 restore resurfaces every approved item as pending; committed, two concurrent sessions produce conflicting writes to the same file. Per-commit notes under a dedicated ref are append-only, merge without textual conflict, and push and restore with the repo for free.
- **Leak and privacy checks are fatal; durability checks warn.** `ExecStartPre` failure blocks service start, and `StartLimitBurst=5` means repeated failure leaves the unit `failed` until manual intervention. A missing remote is a degraded-durability condition and must not convert into a total assistant outage; a secret in the checkout or an unverified-private remote must.
- **The private remote is created by the owner, never by the assistant.** Creating that repo is the one Bash action where a wrong flag publishes the knowledge base. Mirrors `make setup`'s existing refusal to automate inherently-manual steps.
- **R2's carrier is a tracked `CLAUDE.md`.** Nothing the assistant needs to know is secret. Rejected: `CLAUDE.local.md` (gitignore-only, violates R3) and cross-boundary `@~/...` imports (external imports trigger an approval dialog a headless session cannot answer).
- **R7 is best-effort bounded by remote reachability.** As originally worded it is unsatisfiable. Fine-grained tokens expire (30 days by default), so silent push failure is an expected steady state, not an edge case — which is why backlog visibility is built rather than deferred.

---

## Open Questions

### Resolved During Planning

- *Commit granularity (affects R5):* one commit per logical hub change.
- *"Changed since last review" (affects R9):* per-commit disposition via git notes.
- *Structural barrier (affects R3):* extend the existing `ExecStartPre` preflight — the one control that exists on deployments.
- *Pull vs push review (affects R12):* pull-only this pass.
- *Hub path carrier (affects R2):* tracked `CLAUDE.md` plus `BUZAI_HUBS_DIR`, set in the unit rather than a shell profile.
- *Secrets mechanism (affects R15/R17):* unchanged — systemd `EnvironmentFile` at `~/.config/buzai/secrets/`.
- *Deployment-side backstop (affects R3):* the extended preflight, wired into `ExecStartPre`, `make doctor`, and smoke SC0.
- *Migration:* explicit step in U2; "no migration burden" was a point-in-time observation and `docs/SETUP.md` §9 currently tells adopters to populate hubs in-checkout.
- *Credential transport:* SSH deploy key with pre-populated `known_hosts`. Never a token embedded in a remote URL — git echoes the remote on failure and the unit sends stderr to the journal, which would write the token to logs in plaintext on exactly the failures this design expects routinely.

### Deferred to Implementation

- Lock acquisition timeout value and the push network-timeout constants.
- Whether the credential-detection rule set is vendored or shelled out to an installed scanner.
- Backlog age threshold at which the review summary prepends a warning.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

```
PUBLIC repo (system dir, safe to git pull)      PRIVATE hub repo (personal, versioned)
~/buzai/                                        $BUZAI_HUBS_DIR (default ~/hubs)
  CLAUDE.md              tracked, generic  ───▶   seeded once by `make hub-init`
  hubs/README.md         template                 finance-and-tax.md, home.md …
  hubs/_example-hub.md   template                 refs/notes/review  (dispositions)
  deploy/env/*.env.example  template              .git ──▶ verified-PRIVATE remote
  scripts/, trust/, docs/  machinery                        (ssh deploy key)

                                                NO repo above it
                                                ~/.config/buzai/secrets/*.env  (0600)

  write path:  [lock] ─▶ read ─▶ modify ─▶ atomic write ─▶ credential scan ─▶ commit(+trailer) ─▶ [unlock] ─▶ push (timeout-bounded)
                                                              │                                                    └─ fail: backlog, never blocks
                                                              └─ match: roll back the write, refuse, report in-turn
  review path: git notes ─▶ plain-language summary ─▶ approve / reject(+reason[, value]) ─▶ remove-or-replace ─▶ commit
```

---

## Implementation Units

- U1. **Hub location convention and its carrier**

**Goal:** Establish where hubs live and how both the assistant and the scripts resolve that path.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Create: `CLAUDE.md`
- Create: `scripts/hub_paths.py`
- Create: `scripts/tests/test_hub_paths.py`
- Modify: `hubs/README.md`

**Approach:**
- `CLAUDE.md` carries generic, public-safe content only: the hub path convention, the rule that hub content must never be written inside the checkout, commit discipline, and review-loop behavior.
- `scripts/hub_paths.py` is the single resolver: reads `BUZAI_HUBS_DIR`, defaults to `~/hubs`, and refuses any path that resolves inside the repo checkout **after** symlink resolution — a `~/hubs` symlink pointing into the checkout must fail, not pass a lexical check.
- Every consumer prints the resolved path so a misconfiguration is visible in the journal rather than silent.

**Patterns to follow:**
- `scripts/trust_check.py` module shape; module-level path constants as in `scripts/secrets_preflight.py`.

**Test scenarios:**
- Happy path: unset `BUZAI_HUBS_DIR` resolves to `~/hubs`; a custom value resolves with `~` expanded.
- Error path: a value inside the repo checkout is refused with a clear message.
- Error path: a symlink whose target is inside the checkout is refused — the check resolves symlinks before comparing.
- Edge case: a relative value resolves against a defined base, not the ambient cwd.
- Edge case: an empty-string value is treated as unset, not as the filesystem root.

**Verification:**
- The resolver is the only place a hub path is computed, and every caller logs what it resolved to.

---

- U2. **`make hub-init` — instantiate the private hub repo from the public template**

**Goal:** Turn the tracked scaffolds into a working private hub repository, locally and idempotently.

**Requirements:** R1, R2, R8

**Dependencies:** U1

**Files:**
- Create: `scripts/hub_init.py`
- Create: `scripts/tests/test_hub_init.py`
- Modify: `Makefile`

**Approach:**
- Create the resolved directory, `git init`, seed from `hubs/` scaffolds, write an initial README describing this instance's store, make the initial commit, and record a `remote_expected` marker with the creation timestamp (U6 uses it; see below).
- Stop before remote creation. Print the exact owner-run commands, in the style `make setup` uses at its manual pauses.
- Idempotent and non-destructive: an existing hub repo is reported, never re-seeded.
- One-time migration: real hub markdown already in `hubs/` inside the checkout is moved into the new repo and removed from the checkout.

**Patterns to follow:**
- `scripts/install_hooks.py` refuse-to-clobber semantics; `Makefile` verb conventions (`## ` help, `.PHONY`, `# --- build / setup ---` group).

**Test scenarios:**
- Happy path: empty target — directory created, repo initialized, scaffolds seeded, one initial commit, marker written.
- Happy path: `Covers AE4.` after init plus a remote, a clone of the remote reproduces content and history.
- Edge case: second run is a no-op reporting the existing repo.
- Edge case: target exists as a non-empty non-repo directory — refuses rather than initializing over it.
- Edge case: migration — real hub files move out of the checkout, the checkout keeps only scaffolds, moved content is in the initial commit.
- Error path: unwritable target, or `git` unavailable — fails cleanly with no partial repo left behind.

**Verification:**
- A fresh instance reaches a committed local hub repo in one verb; re-running changes nothing.

---

- U3. **Private-remote wiring and privacy verification**

**Goal:** Ensure nothing is ever pushed to a remote that has not been confirmed private.

**Requirements:** R3, R4, R7

**Dependencies:** U2

**Files:**
- Create: `scripts/hub_remote.py`
- Create: `scripts/tests/test_hub_remote.py`
- Modify: `Makefile`
- Modify: `deploy/README.md`

**Approach:**
- Verify privacy by anonymous readability probe: an unauthenticated `git ls-remote` that succeeds proves the repo is public. No `gh`, no API token, no new dependency.
- Cache the verdict with a TTL; re-verify at service start, on TTL expiry, and on URL change. An expired cache refuses and queues rather than pushing.
- Assert the **public** origin still has no credential helper — the assumption the whole barrier rests on is now something this plan could break, so it is checked rather than assumed.
- Document the credential as an SSH deploy key scoped to the hub repo, with `known_hosts` pre-populated. All git invocations run non-interactively (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`) so a missing host key fails fast instead of hanging a TTY-less service.

**Execution note:** Implement the refusal paths test-first — a permissive default would be invisible in normal use and is the control the whole privacy story rests on.

**Patterns to follow:**
- Injected-callable testing per `scripts/tests/test_trust_check.py`; non-crashing returncode handling as in `scripts/secrets_preflight.py::_git_tracked`.

**Test scenarios:**
- Happy path: anonymous probe fails with auth-required and the authenticated probe succeeds — verified private, push permitted.
- Error path: anonymous probe succeeds — repo is public, push refused, remote named in the message.
- Error path: `Covers R3.` remote flipped private→public with an unchanged URL — the next push after TTL expiry is refused.
- Error path: both probes fail (host unreachable) — refuses and queues; does not fall through to "assume private."
- Error path: a credential helper is configured for the public origin — reported as a violation.
- Edge case: no remote configured — reported as local-only, distinctly from "public."
- Edge case: a hung probe is bounded by timeout rather than blocking indefinitely.

**Verification:**
- A deliberately public test remote is refused, proven by a test that fails if the check defaults permissive.

---

- U4. **Hub write, commit, and push path**

**Goal:** The single-writer core the assistant invokes to record knowledge durably and reviewably.

**Requirements:** R5, R6, R7, R11, R14

**Dependencies:** U1, U3

**Files:**
- Create: `scripts/hub_commit.py`
- Create: `scripts/tests/test_hub_commit.py`
- Modify: `Makefile`

**Approach:**
- The API takes an **operation** (append entry / replace section / remove entry), not pre-rendered file content, so the read happens inside the critical section.
- The exclusive lock spans read → modify → atomic write → credential scan → commit. The push runs after release, bounded by an explicit network timeout.
- Atomic write via temp-then-`Path.replace()`. If the credential scan matches, the write is rolled back to prior content and the refusal is reported in-turn — the only behavior consistent with "commit is the durability boundary."
- Stale lock recovery for a lock held by a dead process; a live holder causes queuing bounded by an acquisition timeout.
- Commit messages state what changed and why, plus a trailer naming the instruction source (autonomous / owner-directed / owner-correction) and a distinct assistant git author identity.
- A dirty hub working tree at entry is reconciled, not ignored: content is committed with an `instruction-source: unattributed` trailer so it becomes visible to review rather than invisible on disk.
- Push failure is non-fatal and recorded in the backlog; retry occurs on next write **and** at service start. Divergence halts and surfaces; remote content is never merged into local files.

**Execution note:** Write the concurrency test first, asserting final *file content* contains both facts — a naive implementation passes a two-commits-exist assertion while silently losing one update.

**Patterns to follow:**
- `trust/provenance.py` write-temp-then-replace; explicit `-C`/`cwd` git targeting; non-crashing returncode checks.

**Test scenarios:**
- Happy path: one hub write produces exactly one commit, message states what changed, trailer and author identity present.
- Happy path: `Covers AE3.` remote unreachable — write reports success, commit exists locally, backlog records it, later retry pushes it.
- Integration: two concurrent writers on the same file — **the final file contains both facts**, and history contains both commits.
- Integration: a push that hangs does not block a second session's write from committing within the lock timeout.
- Edge case: stale lock from a dead process is cleared; a live holder queues rather than fails.
- Error path: credential scan matches a planted realistic token — commit refused and the file rolled back to prior content.
- Error path: commit fails (unset identity, disk full) — surfaced as failure, never reported as a successful write.
- Edge case: dirty hub tree at entry — pre-existing changes are committed as unattributed and become visible to review.
- Integration: remote diverged — the operation halts and surfaces; no merge, no force-push, no discarded remote commit.

**Verification:**
- Concurrency, hung-push, stale-lock, and scan-rollback scenarios each fail against a naive implementation.

---

- U5. **Review state and the "what changed" loop**

**Goal:** Let the owner see what the assistant recorded, judge it per item, and have rejections resolved — from the Claude apps.

**Requirements:** R9, R10, R11, R12

**Dependencies:** U4

**Files:**
- Create: `scripts/hub_review.py`
- Create: `scripts/tests/test_hub_review.py`
- Modify: `Makefile`
- Modify: `CLAUDE.md`

**Approach:**
- Disposition recorded as a git note per commit under a dedicated ref — append-only, conflict-free across concurrent sessions, pushed and restored with the repo.
- "What changed" returns undisposed commits with their message text, never raw diffs. Un-acted items stay pending; partial review never auto-advances.
- Rejection carries the owner's reason and optionally a correct value. With a value, the content is replaced; without one, the rejected content is **removed**. The reason is recorded in the correction commit trailer so R11's trail shows why.
- The summary prepends a warning when the oldest unpushed commit exceeds a staleness threshold, so a silent backlog surfaces without any scheduling machinery.

**Test scenarios:**
- Happy path: `Covers AE2.` three changes, owner rejects one with a reason — content removed, correction committed, original and correction both in history.
- Happy path: approving marks it and it does not reappear next query.
- Happy path: rejecting with a supplied value replaces rather than removes.
- Edge case: partial review — approving two of three leaves the third pending.
- Edge case: two sessions dispose concurrently — notes merge without conflict and no item is double-applied.
- Edge case: rejecting a change from twenty commits ago whose text was since reworded — removal resolves without needing the original hunk.
- Edge case: no changes since last review — reports so cleanly.
- Edge case: unpushed backlog older than the threshold — the summary leads with the warning.
- Error path: notes ref missing or unreadable — failed loudly, never treated as "everything approved."

**Verification:**
- Nothing is ever marked reviewed that the owner did not act on, and no rejection produces invented content.

---

- U6. **Structural preflight for personal state**

**Goal:** Make the layout guarantees enforceable on the deployment, where pre-commit hooks do not exist.

**Requirements:** R3, R15, R16, R17

**Dependencies:** U1

**Files:**
- Modify: `scripts/secrets_preflight.py`
- Modify: `scripts/tests/test_secrets_preflight.py`
- Modify: `deploy/claude-remote.service.template`
- Modify: `Makefile`

**Approach:**
- Extend the existing `ExecStartPre` gate — the one control that already runs on deployments.
- **Fatal checks:** `hubs/` in the checkout contains only tracked scaffolds; no real `.env` under `deploy/env/`; the hub credential is mode 0600 and untracked by any repo; no credential helper configured for the public origin.
- **Warning checks (never block start):** hub repo has commits older than the `remote_expected` grace period with no remote configured; unpushed backlog count and age; dirty hub working tree. These are durability conditions — with `StartLimitBurst=5`, making them fatal converts degraded knowledge into a permanent outage.
- Add `Environment=BUZAI_HUBS_DIR=%h/hubs` (commented) to the unit template and document that the override belongs in the unit, never only in a shell profile — a `--user` unit sources neither `.profile` nor `.bashrc`, so a profile-only override would silently diverge the assistant's store from the owner's.
- Always print the resolved hub path so a mismatch is visible in the journal.
- Tolerate legitimate empty states — a fresh or scaffold-only instance is not a violation.

**Execution note:** Characterization-first — pin the current behavior of `problems()` and `tracked_problems()` before extending; this script gates service start.

**Test scenarios:**
- Happy path: a correct instance passes; scaffold-only hubs and zero local secrets pass.
- Error path: a real hub markdown file planted in `hubs/` fails, naming the file.
- Error path: a real `.env` planted under `deploy/env/` fails, naming the file.
- Error path: hub credential with loose perms, or tracked by a repo, fails.
- Error path: a credential helper configured for the public origin fails.
- Warning path: hub repo older than the grace period with no remote — warns with the unpushed count, and **start is not blocked**.
- Warning path: dirty hub working tree — warned, not fatal.
- Edge case: existing secrets perm checks still fire unchanged.

**Verification:**
- Every new check has a test that plants the violation and proves it fires; every warning check has a test proving it does *not* block start.

---

- U8. **Documentation, ADR, secrets consolidation, and decision correction**

**Goal:** Make the new layout discoverable, complete the secrets move, and correct the record it contradicts.

**Requirements:** R8, R15, R16, R17, and documentation of R1–R14

**Dependencies:** U2, U3, U4, U5, U6

**Files:**
- Create: `docs/decisions/private-versioned-hubs.md`
- Modify: `docs/SETUP.md`
- Modify: `docs/SMOKE-TEST.md`
- Modify: `docs/TROUBLESHOOTING.md`
- Modify: `docs/decisions/knowledge-management.md`
- Modify: `deploy/README.md`
- Modify: `hubs/README.md`
- Modify: `.gitignore`

**Approach:**
- Secrets consolidation is documentation plus a one-time move: `~/.config/buzai/secrets/*.env` becomes the only location for real values, `deploy/env/` keeps only tracked `.example` scaffolds, and the U6 preflight enforces it. On the current instance this is a no-op — `~/.config/buzai/` does not exist and the setup is managed-connector-only. *(Folded here rather than a separate unit: it adds no code path of its own.)*
- New ADR mirroring the `docs/decisions/knowledge-management.md` skeleton, recording the private-remote design, the restated R13 with its accepted un-legged outbound flow, and the alternatives rejected.
- `docs/SETUP.md` §9 rewritten around the new layout, including `make hub-init`, the owner-run remote step, and the single-step restore (R8).
- `docs/SMOKE-TEST.md`: extend SC6 to assert off-box push as well as persistence; add a review-loop criterion.
- `docs/TROUBLESHOOTING.md`: symptom-first entries for a stale push backlog (naming token expiry as the expected trigger), a refused-because-public remote, and "the assistant can't find my hubs."
- Correct the stale "Preserved from the PoC: … the Drive mirror" claim, which asserts an implementation that never shipped.

**Test scenarios:**
- `Covers AE5.` With a real secret configured, inspection of the whole checkout finds no secret value — enforced by the U6 preflight tests.
- Otherwise documentation only; behavior is covered by U2–U6.

**Verification:**
- A reader can go from a fresh instance to a working private hub store using only the docs, and no doc still claims the Drive mirror exists.

*(U7 was folded into U8 during review; the U-ID is retired rather than reused, per the stability rule.)*

---

## System-Wide Impact

- **Interaction graph:** trust gate hooks untouched. `ExecStartPre` gains checks, so preflight correctness becomes load-bearing for availability — mitigated by the fatal-vs-warning split.
- **Error propagation:** push failures queue; commit failures surface in-turn; scan refusals roll back the write; leak/privacy preflight failures block start loudly; durability conditions warn.
- **State lifecycle risks:** concurrent same-dir sessions, stale locks, hung pushes, unpushed backlog, dirty working tree, and remote divergence each have a named behavior above.
- **API surface parity:** new Makefile verbs are additive only.
- **Integration coverage:** concurrency, hung-push, divergence, and restore cannot be proven by unit tests alone and are called out per unit.
- **Unchanged invariants:** hub shape, hub-wins precedence, search-not-embeddings retrieval, and gate configuration all stay as they are.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Private remote created public by mistake | Owner-driven creation; push refused until an anonymous-readability probe proves it non-public, failing closed |
| Remote flipped to public later with the same URL | TTL-bounded cache re-verified at service start; expiry refuses rather than pushes |
| Verification mechanism widens credentials to the public origin | No `gh` and no API token; preflight asserts the public origin has no credential helper |
| Concurrent sessions silently clobber a hub update | Lock spans read→write→commit; test asserts final file content, not just commit count |
| A hung push blocks all writes | Push runs outside the lock, timeout-bounded, non-interactive |
| Credential leaked into journal logs | SSH deploy key only; never a token embedded in a remote URL |
| Assistant pastes a credential into hub content | Credential/entropy scan (not PII patterns) with rollback on match, and a plant-a-token test |
| New preflight checks are dead no-ops | Every check has a plant-a-violation test |
| Durability check takes the assistant down | Durability conditions warn; only leak/privacy conditions are fatal |
| Content written outside the commit path is invisible to review | Dirty-tree reconciliation in U4 plus a preflight warning |
| Rejection produces invented content | Default action is removal; replacement only when the owner supplies a value |
| Owner never creates the remote, so nothing is off-box | Grace-period warning naming the unpushed count |

---

## Documentation / Operational Notes

- **Rollout order on the live instance:** U1 → U2 → U3 (verify against a real private repo before any write path exists) → U4 → U5 → U6 → U8. U6 lands late deliberately: it changes service-start behavior, so it should follow the units whose state it inspects.
- Both migrations are effectively no-ops on the current instance — hubs hold only scaffolds and `~/.config/buzai/` does not exist.
- After U1 ships, `git pull` delivers `CLAUDE.md` before `make hub-init` has necessarily run. Until init completes, the assistant sees an empty hub location while hub-wins precedence consults it — run `make hub-init` in the same maintenance window.
- After U6 lands, verify on the instance that a warning condition does not block start before relying on the service unattended.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-08-04-private-versioned-hubs-requirements.md](docs/brainstorms/2026-08-04-private-versioned-hubs-requirements.md)
- Related decisions: `docs/decisions/knowledge-management.md`
- Related code: `scripts/secrets_preflight.py`, `scripts/trust_check.py`, `scripts/install_hooks.py`, `scripts/publish_gate.py`, `trust/provenance.py`, `deploy/claude-remote.service.template`
- Related learnings: `docs/solutions/architecture-patterns/running-claude-code-always-on.md`, `docs/solutions/best-practices/verify-collaboration-tool-access-model-2026-06-28.md`
