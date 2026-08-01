---
date: 2026-06-27
topic: trifecta-lifecycle-and-unknown-tool
---

# Trifecta Leg-State Lifecycle & Unknown-Tool Default — Requirements

## Problem Frame

The trust gate's Rule-of-Two check evaluates the three trifecta legs (private-read,
untrusted-content, external-send) against a per-session accumulated set
(`trust/legstate.py`). That set is **monotonic and session-lifetime-scoped**: it only
ever unions, and nothing resets it short of the process dying. For an always-on,
single-principal assistant that runs as one long-lived session for days, this is a
self-inflicted denial of service:

- The **first** owner-approved `external_send` (e.g. a calendar invite) lights the send
  leg permanently.
- **Any later** read of inbound content lights the untrusted leg.
- From that point every private read *and* every send completes all three legs and is
  **hard-denied** (`trust/gate.py:144`) until the owner restarts the session. The
  assistant self-bricks mid-day. (Reproduced live in the 2026-06-27 smoke test — issue #7.)

The same monotonic-accumulation disease has a second carrier: an **unmapped tool**.
`classify.classify()` returns all three legs for any tool matching no `connector-legs`
rule (fail-conservative), and accumulation then makes those legs sticky. So a single
call to *any* unrecognized harness/MCP tool — a new built-in, a deferred-tool loader,
the next managed connector — bricks the session on first use (issue #6.1; hit twice live).

Both are the **same failure mode**: legs accumulate forever and a wrong/too-broad leg
assignment is therefore permanent. This brief fixes the lifecycle so neither bricks the
assistant, **without weakening the core guarantee: untrusted content must never silently
weaponize an existing send capability.**

```
Today:   [session start] --legs only ever union--> [all 3 legs lit] --> DENY everything --> [restart]
Fixed:   [turn start: legs reset] --accumulate within turn--> Rule-of-Two on THIS turn's legs
         owner-tier completion -> loud ASK (human decides) ; non-owner -> DENY (unchanged)
```

---

## Actors

- A1. Owner — the single principal; the sole tier-0 channel. Already individually
  gated (ASK) on every `external_send`, so they are the human validator on the bottom of
  every privileged action (the "AI Sandwich" bottom bread).
- A2. Assistant — the Claude Code agent whose tool calls the gate evaluates; never
  inherits trust from content it reads.
- A3. Non-owner tiers — configured-automation / known-contact / untrusted-external
  turns. No human validator is present on an autonomous loop, so their handling stays
  strict.

---

## Key Flows

- F1. Long-lived owner session, unrelated tasks across turns (the self-brick, fixed)
  - **Trigger:** Owner approves a calendar invite (send leg), then in a later turn runs
    a read-only inbox search.
  - **Actors:** A1, A2
  - **Steps:** Turn 1 accumulates `external_send`; turn ends. Turn 2 starts → legstate
    reset → the inbox search accumulates only `private_read` + `untrusted_content` →
    two legs, not three → not denied.
  - **Outcome:** The search runs without a restart. The earlier approved send does not
    poison the later turn.
  - **Covered by:** R1, R2

- F2. In-turn trifecta at owner tier (the genuine risk, kept in the loop)
  - **Trigger:** Within one turn the assistant reads a private doc, ingests an untrusted
    email, and attempts a send.
  - **Actors:** A1, A2
  - **Steps:** All three legs accumulate within the turn → Rule-of-Two completes → at
    owner tier the gate emits a **loud ASK** naming the three legs and the injection risk
    → owner approves or rejects.
  - **Outcome:** The send happens only on an explicit, informed owner decision; the
    audit entry is marked a trifecta-warning approval.
  - **Covered by:** R3, R4, R6

- F3. In-turn trifecta at a non-owner tier (no human bread — unchanged)
  - **Trigger:** An automation/known-contact/untrusted turn completes all three legs.
  - **Actors:** A3, A2
  - **Steps:** Rule-of-Two completes → **hard-DENY** with the decompose-and-reapprove
    reason (today's behavior).
  - **Outcome:** No autonomous loop can self-complete a trifecta.
  - **Covered by:** R3, R5

- F4. Unmapped tool, first use (no longer bricks)
  - **Trigger:** The assistant calls a tool matching no `connector-legs` rule.
  - **Actors:** A2
  - **Steps:** The tool contributes **zero legs** to accumulation; the decision falls to
    standing policy, which defaults unmatched classes to review → **ASK** once.
  - **Outcome:** The owner approves the unrecognized tool once; nothing auto-runs and the
    rest of the turn/session is not bricked.
  - **Covered by:** R7

---

## Requirements

**Leg-state lifecycle**
- R1. Trifecta leg-state is scoped to a single turn: it resets at each turn boundary
  (the `UserPromptSubmit` provenance point, `trust/mark_turn.py`) so legs from a prior,
  completed turn never count toward the current turn's Rule-of-Two evaluation.
- R2. Within a turn, legs still accumulate across that turn's tool calls (union,
  sticky-untrusted unchanged), so a read-then-act sequence inside one turn is evaluated
  against the union — the threat property is preserved at the turn granularity.

**Rule-of-Two completion response (tier-dependent)**
- R3. The reset (R1/R2) applies at every tier; only the *response to a completed
  trifecta* is tier-dependent.
- R4. At owner tier, a call that would complete all three legs produces a **loud ASK**
  (not a hard-DENY) carrying a distinct reason that names the three accumulated legs and
  warns that untrusted content may be steering the action — so the owner decides with
  full context.
- R5. At every non-owner tier, a completed trifecta remains a **hard-DENY** with the
  existing decompose-and-reapprove reason. There is no human validator on an autonomous
  loop, so the automated refusal stays.

**Unknown-tool default**
- R7. A tool matching no `connector-legs` rule contributes **zero trifecta legs** (it
  does not poison accumulation) and is **not auto-run** — it resolves to ASK via the
  existing default-review policy. This replaces today's all-three-legs → hard-DENY
  default. (Known send/read tools stay explicitly mapped; this changes only the
  *unrecognized* case.)

**Auditability**
- R6. A trifecta-warning approval (R4) is recorded as a higher-signal audit event,
  distinguishable from an ordinary policy-review ASK, so log review can find every time
  the owner approved through a completed trifecta.

---

## Acceptance Examples

- AE1. **Covers R1, R2.** Given an owner session that approved a calendar invite in an
  earlier turn, when a later turn runs only a read-only inbox search, then the search is
  permitted (two legs this turn) without a session restart.
- AE2. **Covers R4, R6.** Given an owner turn that has read a private file and ingested
  an untrusted email, when it attempts an external send in the same turn, then the gate
  asks with a reason naming the three legs and the injection risk, and the resulting
  approval is audited as a trifecta-warning approval.
- AE3. **Covers R5.** Given the same in-turn trifecta on a non-owner-tier turn, when the
  send is attempted, then the gate hard-denies with the decompose-and-reapprove reason.
- AE4. **Covers R7.** Given a call to a tool that matches no `connector-legs` rule, when
  it is the first such call, then the gate asks once and does not deny it or any
  subsequent call in the session solely because the tool was unmapped.
- AE5. **Covers R1, R2, R4.** Given the injection scenario — an untrusted email
  instructing the assistant to send a private document to an external address, all within
  one turn — when the send is attempted, then it is gated (loud ASK at owner tier;
  DENY at non-owner) and cannot complete without an explicit human decision.

---

## Success Criteria

- A long-lived owner session can approve a send and later run unrelated reads across
  many turns over a full day with **no self-brick and no restart** to recover gate
  function.
- A genuine within-turn trifecta is still stopped: at owner tier it always surfaces a
  loud, leg-naming ASK; at non-owner tiers it is denied. Untrusted content cannot
  complete a send without an informed human decision (the threat property holds).
- A first call to any unmapped tool asks once and never bricks the session.
- The audit log distinguishes trifecta-warning approvals from ordinary ASKs.
- Downstream: `trust/` unit tests cover turn-reset, in-turn accumulation, owner-vs-non-owner
  completion, and the unmapped-tool path; the documented KTD reversals (below) are
  recorded so the security-stance change is deliberate and reviewable.

---

## Scope Boundaries

- **Multi-turn "task" boundary (option B)** — detecting task end via idle/topic-change
  to span a coherent task across turns. Rejected for MVP: fuzzy, stateful, error-prone;
  a turn is the natural autonomous unit and the owner re-enters at each prompt.
- **Leg TTL / decay (option C)** — time- or count-based leg expiry. Rejected: arbitrary
  window, can still brick inside it, weaker (time-based not causal) security story.
- **Leg "consumption" on approved send (option D)** — made moot by turn-scoping; not built.
- **Active escalation / re-confirmation on repeated trifecta-warning approvals (#7 Q4)**
  — deferred. MVP records them distinctly in the audit log (R6) but adds no automatic
  rubber-stamp guard.
- **Rest of #6** — Bash containment (#6.2) and the protected-config edit-authority edge
  (#6.3) are out of this brief (deferred post-MVP).
- **Non-owner provenance mechanics** — how automation/inbound turns get their per-turn
  boundary and tier is the existing provenance writer's concern, not redesigned here.

---

## Key Decisions

- **Turn-scoped leg-state (A), not multi-turn or TTL.** A turn is the autonomous unit
  Rule-of-Two should protect; the owner is the bread between turns. Reset at the existing
  `UserPromptSubmit` provenance boundary. Resolves issue #7 (a).
- **Ship (a) with (c).** (a) alone unbricks across turns but leaves the legitimate
  within-turn "triage and act" case hard-denied at owner tier. (c) — owner-tier
  completion → loud ASK — makes it usable while keeping the human in the loop, grounded in
  the AI-Sandwich principle (escalate attention, don't remove the human). Non-owner tiers
  keep hard-DENY because no human is present on an autonomous loop.
- **Decouple unknown-tool classification from decision.** Unmapped → zero legs (don't
  poison the trifecta accounting) + ASK (don't auto-run), instead of max-legs → DENY.
  Safe-by-default is preserved as "unrecognized → ask the owner," not "assume the worst
  and brick." Resolves #6.1.
- **Documented KTD reversals — deliberate.** This revises the shipped stances that
  (i) "an unknown tool fails conservative to all three legs" and (ii) leg accumulation is
  session-lifetime monotonic. Both reversals are intentional and must be recorded in
  `trust/README.md` / KTD notes so the security posture change is explicit.

---

## Dependencies / Assumptions

- The `UserPromptSubmit` hook (`trust/mark_turn.py`) fires once per turn and is the
  legstate reset site; it already runs in production as the provenance writer. Verified
  against the codebase.
- `trust/legstate.py` keys state on the hook's `session_id`; turn-scoping needs a
  per-turn reset (clear or re-key) at that boundary — exact mechanism is a planning detail.
- Single-principal MVP: in practice all turns are owner tier today, so (c) is the common
  path and non-owner DENY is the forward-looking guard.
- The injection threat model and the sticky-untrusted-within-turn behavior are unchanged
  from the trust-capability requirements (`docs/brainstorms/2026-06-23-trust-capability-model-requirements.md`).

---

## Outstanding Questions

### Resolve Before Planning

- *(none — all product decisions resolved in this brief.)*

### Deferred to Planning

- [Affects R1][Technical] The exact reset mechanism at the turn boundary — does
  `mark_turn.py` clear the session's legstate file, or does the gate re-key per turn
  (e.g. session_id + turn counter)? Resolve against atomicity/ordering with the
  provenance write.
- [Affects R4][Technical] The loud-ASK reason string shape and how the audit
  trifecta-warning marker is represented (`GateResult` field vs. reason convention).
- [Affects R7][Technical] Whether "zero legs for unmapped" is implemented in
  `classify.classify()` or as a gate-level guard, and confirming the policy default-review
  path yields ASK for unmapped tool names.

---

## Next Steps

→ `/ce-plan` for structured implementation planning.
