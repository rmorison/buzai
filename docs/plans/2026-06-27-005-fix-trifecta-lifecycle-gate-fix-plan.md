---
title: "fix: Turn-scoped trifecta leg-state, owner-tier loud-ASK, and unknown-tool default"
type: fix
status: completed
date: 2026-06-27
origin: docs/brainstorms/2026-06-27-trifecta-lifecycle-and-unknown-tool-requirements.md
---

# fix: Turn-scoped trifecta leg-state, owner-tier loud-ASK, and unknown-tool default

## Overview

The trust gate's Rule-of-Two evaluates the three trifecta legs against a per-session
accumulated set that is **monotonic and never resets** (`trust/legstate.py`). On a
long-lived always-on session this self-bricks: the first approved `external_send` plus
any later untrusted read permanently lights all three legs, and every subsequent
private read or send is hard-denied until restart. The same monotonic-accumulation
disease has a second carrier — an unmapped tool is classified as all three legs
(`trust/classify.py`), so one call to any unrecognized tool bricks the session.

This plan scopes leg-state to the **turn** (resetting at the existing `UserPromptSubmit`
boundary), relaxes a completed trifecta at **owner tier** from hard-DENY to a **loud
ASK** that names the three legs (keeping non-owner tiers strict), and changes the
**unknown-tool default** to contribute **zero legs + ASK**. Throughout, the core
guarantee is preserved: untrusted content cannot silently complete a send without an
informed human decision.

---

## Problem Frame

See origin: `docs/brainstorms/2026-06-27-trifecta-lifecycle-and-unknown-tool-requirements.md`.
Reproduced live in the 2026-06-27 MVP smoke test (issues #7 and #6.1). Both bricks are
the same failure mode — legs accumulate forever, so a too-broad or stale leg assignment
is permanent. The fix is a lifecycle change, not a per-tool config patch.

---

## Requirements Trace

- R1. Trifecta leg-state is scoped to a single turn; it resets at the `UserPromptSubmit`
  boundary so a prior turn's legs never count toward the current turn.
- R2. Within a turn, legs still accumulate across that turn's tool calls (union,
  sticky-untrusted unchanged).
- R3. The reset applies at every tier; only the response to a completed trifecta is
  tier-dependent.
- R4. At owner tier, a call completing all three legs produces a loud ASK naming the
  three legs and the injection risk.
- R5. At every non-owner tier, a completed trifecta stays a hard-DENY.
- R6. A trifecta-warning approval is recorded as a higher-signal, distinguishable audit
  event.
- R7. An unmapped tool contributes zero trifecta legs and resolves to ASK (via the
  existing default-review policy), not all-three-legs → DENY.

**Origin actors:** A1 (owner), A2 (assistant), A3 (non-owner tiers)
**Origin flows:** F1 (cross-turn unrelated tasks), F2 (in-turn trifecta at owner), F3 (in-turn trifecta non-owner), F4 (unmapped tool first use)
**Origin acceptance examples:** AE1 (covers R1, R2), AE2 (covers R4, R6), AE3 (covers R5), AE4 (covers R7), AE5 (covers R1, R2, R4)

---

## Scope Boundaries

- Multi-turn "task" boundary detection (origin option B) — rejected; turn is the unit.
- Leg TTL / decay (option C) — rejected.
- Leg "consumption" on approved send (option D) — moot under turn-scoping.
- Active escalation / re-confirmation on repeated trifecta-warning approvals (#7 Q4) —
  audit records them (R6) but no automated rubber-stamp guard.
- Rest of #6 — Bash containment (#6.2), protected-config edit-authority edge (#6.3).
- Multi-session / multi-process provenance sharing — single-principal, single process
  assumed; concurrent sessions sharing one provenance file are out of scope.

### Deferred to Follow-Up Work

- Stale per-turn leg-state file cleanup in `.buzai/legstate/` — re-keying per turn
  leaves prior-turn files on disk. Harmless (never re-read) but unbounded; a sweep
  (e.g. prune on turn boundary, or keep only the current turn's file) is a follow-up.

---

## Context & Research

### Relevant Code and Patterns

- `trust/gate.py` — orchestration; step 5 (`if ALL_LEGS <= acc:` at line ~144) is the
  DENY site that branches by tier; `gate()` builds the audit dict in the `finish()`
  helper; `legstate.accumulate(session_id, this_legs, cfg.state_dir)` is the keying site.
- `trust/legstate.py` — `accumulate(session_id, ...)` keys state files by a sanitized
  `session_id`; union-only, no reset path.
- `trust/mark_turn.py` — the `UserPromptSubmit` hook; calls `write_provenance({"kind": kind})`
  once per turn. The natural place to stamp a per-turn id.
- `trust/provenance.py` — single overwritten per-turn file; shape is `{channel?, kind?}`,
  all optional, absence fail-closes. `read_provenance()` is already called each tool call
  by the gate. Add an optional `turn_id`.
- `trust/classify.py` — `classify()` returns `set(ALL_LEGS)` for an unmapped tool (line
  ~37). The single line to change for R7.
- `trust/policy.py` — `tier_for()` defaults unmatched classes to `REVIEW` → the gate maps
  REVIEW to ASK; this is the path an unmapped tool falls through to once it carries no
  legs. `action_class()` defaults to the tool name.
- `trust/redact.py` — `redact_entry()` keeps verdict fields in `_VERBATIM_ENTRY_KEYS`
  legible; a new audit marker field must be whitelisted there to stay readable.
- `trust/audit.py` — `record()` redacts + hash-chains the entry; no change needed beyond
  the gate passing a new field.
- Tests: `trust/tests/test_units.py` (per-module: `TestClassify`, `TestLegState`,
  `TestPolicy`, `TestRedact`, `TestAudit`), `trust/tests/test_gate.py` (integration,
  AE1–AE5, with `GateTestBase` fixtures: `set_provenance`, `cfg`, `run_call`).

### Institutional Learnings

- `docs/solutions/architecture-patterns/running-claude-code-always-on.md` — the always-on
  run model that makes the session-lifetime self-brick a real operational failure (a
  days-long single session), motivating turn-scoping.

### External References

- None — self-contained internal gate logic; no external dependencies or framework docs
  involved. (External research deliberately skipped.)

---

## Key Technical Decisions

- **Reset mechanism = re-key leg-state per turn via a `turn_id` in provenance** (resolves
  the origin's deferred "exact reset mechanism" question). `mark_turn.py` stamps a
  monotonically increasing `turn_id` into the provenance file each `UserPromptSubmit`;
  the gate composes the leg-state scope key as `"{session_id}:{turn_id}"`. A new turn →
  new key → a fresh empty leg set, with no explicit delete (avoids a delete/write race
  against the turn's first `PreToolUse`). Chosen over "clear the legstate file in
  mark_turn" for atomicity.
- **Missing `turn_id` fails closed to session-scoped accumulation.** If provenance has no
  `turn_id` (writer hiccup, hook not yet run), the gate falls back to keying on
  `session_id` alone — the current monotonic behavior — which is the conservative
  direction (over-accumulate, never under-accumulate). Preserves existing AE1 behavior.
- **`turn_id` is a counter, not a timestamp/random.** `mark_turn` reads the prior
  provenance and increments; deterministic and testable, no wall-clock dependency.
- **Owner-tier completion branch lives at gate.py step 5.** `if ALL_LEGS <= acc:` →
  `if tier == Tier.OWNER:` emit `Decision.ASK` with a distinct trifecta-warning reason
  and set an audit marker; `else` keep today's `Decision.DENY` + decompose reason.
- **Audit marker = a dedicated `trifecta_warning: true` field**, whitelisted in
  `redact._VERBATIM_ENTRY_KEYS` so it stays legible and greppable — distinct from the
  generic `reason`/`decision` so log review can find every trifecta approval (R6).
- **Unknown tool → `set()` (zero legs), not `ALL_LEGS`.** Decouples classification from
  decision: the ASK now comes from the policy default-review path, not a leg-driven DENY.
  An explicit, documented reversal of the prior KTD ("unknown tool fails conservative:
  all three legs").

---

## Open Questions

### Resolved During Planning

- Exact reset mechanism → re-key by `turn_id` from provenance (see Key Decisions).
- Where the unmapped-tool ASK comes from → policy default-review (`tier_for` → REVIEW →
  ASK), verified against `trust/policy.py` and `trust/gate.py` step 6.
- Audit distinguishability → dedicated `trifecta_warning` field + redact whitelist.

### Deferred to Implementation

- Exact helper/field names (`turn_id` reader, scope-key builder) and whether the
  composite key is built in `gate.py` or pushed into `legstate.accumulate`'s signature.
- Final loud-ASK reason wording (must name the three legs + injection risk; legibility on
  mobile remote control).
- Whether to prune stale per-turn leg-state files now or defer (currently deferred).

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Leg-state keying — before vs. after:

```
BEFORE:  key = session_id                       # one bucket per session, forever
AFTER:   key = f"{session_id}:{turn_id}"         # one bucket per TURN
         turn_id read from provenance (written by mark_turn each UserPromptSubmit)
         turn_id absent -> key = session_id      # fail-closed: old monotonic behavior
```

Gate step 5 (Rule-of-Two completion) — tier branch:

```
if ALL_LEGS <= acc:                 # all three legs accumulated THIS turn
    if tier == OWNER:
        -> ASK  (reason names the 3 legs + injection risk; audit trifecta_warning=true)
    else:
        -> DENY (decompose-and-reapprove; unchanged)
```

Classification — unknown tool:

```
BEFORE:  no rule match -> {private_read, untrusted_content, external_send}  -> DENY (sticky)
AFTER:   no rule match -> {}  -> falls to policy default-review -> ASK (no leg poison)
```

---

## Implementation Units

- U1. **Per-turn leg-state scoping**

**Goal:** Reset trifecta leg accumulation at each turn by re-keying leg-state on a
per-turn id, ending the cross-turn self-brick (R1, R2).

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Modify: `trust/provenance.py` (document/allow optional `turn_id` in the shape)
- Modify: `trust/mark_turn.py` (read prior provenance, write incremented `turn_id`)
- Modify: `trust/legstate.py` and/or `trust/gate.py` (compose scope key from
  `session_id` + `turn_id`; fall back to `session_id` when `turn_id` absent)
- Test: `trust/tests/test_units.py` (`TestLegState`), `trust/tests/test_gate.py`

**Approach:**
- `mark_turn.main()` reads current provenance, computes `turn_id = prior + 1` (starting
  at 1 / absent→1), writes `{"kind": kind, "turn_id": n}` via `write_provenance`.
- The gate reads `turn_id` from `read_provenance()` (already called) and builds the
  leg-state scope key. Keep `legstate.accumulate` mechanically the same; only the key it
  receives changes.
- Missing/zero `turn_id` → key is `session_id` alone (fail-closed to today's behavior).

**Patterns to follow:** existing atomic `write_provenance` (temp+replace); existing
`_state_path` sanitization in `trust/legstate.py`.

**Test scenarios:**
- Happy path: two calls under the same session but different `turn_id` → second call's
  accumulated set excludes the first call's legs. `Covers AE1.` (reset side)
- Happy path: two calls under the same session and same `turn_id` → legs union (within-turn
  accumulation preserved). `Covers AE5.`
- Edge case: provenance with no `turn_id` → falls back to session-scoped accumulation
  (legs union across calls, today's behavior) — guards AE1's existing same-session test.
- Edge case: `mark_turn` increments correctly across successive calls (1 → 2 → 3), and a
  first turn with no prior provenance starts at 1.
- Integration: full `gate()` call denies/asks based on the current turn's legs only, not a
  prior turn's.

**Verification:** A session that accumulates `external_send` in turn N runs a two-leg read
in turn N+1 without DENY; within-turn accumulation still trips Rule-of-Two.

---

- U2. **Owner-tier trifecta completion → loud ASK; non-owner → DENY**

**Goal:** Branch the Rule-of-Two completion response by tier (R3, R4, R5).

**Requirements:** R3, R4, R5

**Dependencies:** U1 (turn-scoping makes the in-turn trifecta the only one that reaches
this branch; functionally independent but sequenced after)

**Files:**
- Modify: `trust/gate.py` (step 5 tier branch + distinct owner reason string)
- Test: `trust/tests/test_gate.py`

**Approach:**
- Replace the unconditional `ALL_LEGS <= acc → DENY` with a tier check: `Tier.OWNER` →
  `GateResult(Decision.ASK, <trifecta-warning reason>, …)`; all lower tiers → existing
  `Decision.DENY` with the decompose reason.
- Owner reason text names the three accumulated legs and warns untrusted content may be
  steering the action (legible on mobile).

**Patterns to follow:** existing `GateResult(...)` construction and `finish()` audit wrap
in `trust/gate.py`; `Tier.OWNER` comparison as used in `policy.can_edit_protected`.

**Test scenarios:**
- Happy path (owner): in-turn private_read + untrusted_content + external_send → `ASK`
  with a reason mentioning all three legs. `Covers AE2.`
- Happy path (non-owner): same in-turn trifecta at `untrusted-external`/`automation`/`known`
  tier → `DENY`. `Covers AE3.`
- Edge case: owner with only two legs → not forced to the trifecta ASK (falls through to
  normal policy).
- Integration: the injection scenario (untrusted email instructs send of a private doc,
  one turn) → owner ASK / non-owner DENY; never a silent ALLOW. `Covers AE5.`

**Verification:** Owner in-turn trifecta surfaces a loud, leg-naming ASK; non-owner stays
DENY; two-leg owner actions are unaffected.

---

- U3. **Audit marker for trifecta-warning approvals**

**Goal:** Record the owner-tier loud-ASK as a distinguishable, legible audit event (R6).

**Requirements:** R6

**Dependencies:** U2

**Files:**
- Modify: `trust/gate.py` (include `trifecta_warning: true` in the audit dict on the
  owner-tier branch)
- Modify: `trust/redact.py` (add `trifecta_warning` to `_VERBATIM_ENTRY_KEYS`)
- Test: `trust/tests/test_units.py` (`TestRedact`), `trust/tests/test_gate.py`

**Approach:**
- The owner-tier branch passes a `trifecta_warning` flag through to `audit.record`
  (via the `finish()`/entry dict). Ordinary ASKs omit it (or set false).
- Whitelist the key in `redact._VERBATIM_ENTRY_KEYS` so it survives redaction legibly,
  matching how `decision`/`tier`/`legs` are kept.

**Patterns to follow:** `_VERBATIM_ENTRY_KEYS` handling in `trust/redact.py:redact_entry`;
the audit entry assembly in `trust/gate.py:finish`.

**Test scenarios:**
- Happy path: an owner trifecta ASK writes an audit entry with `trifecta_warning: true`
  preserved verbatim after redaction. `Covers AE2.`
- Edge case: an ordinary policy-review ASK has no `trifecta_warning` marker (greppable
  distinction holds).
- Integration: `verify_chain` still passes with the new field present (field is inside the
  hashed entry).

**Verification:** Log review can grep trifecta-warning approvals and they remain legible
and chain-valid.

---

- U4. **Unknown-tool default → zero legs**

**Goal:** Stop an unmapped tool from poisoning leg-state; let it resolve to ASK (R7).

**Requirements:** R7

**Dependencies:** None

**Files:**
- Modify: `trust/classify.py` (return `set()` for an unmapped tool; update docstring)
- Test: `trust/tests/test_units.py` (`TestClassify`), `trust/tests/test_gate.py`

**Approach:**
- Change the `rule is None` branch from `return set(ALL_LEGS)` to `return set()`.
- The gate then sees zero legs for the call; `action_class` defaults to the tool name,
  unmatched in policy → REVIEW → ASK. No leg accumulation, no Rule-of-Two trip.

**Patterns to follow:** existing `classify()` structure; the gate's policy step-6
REVIEW→ASK mapping in `trust/gate.py`.

**Test scenarios:**
- Happy path: `classify()` on a tool matching no rule → empty set (was: all three legs).
  Replaces/retargets the existing `test_unknown_tool_fails_conservative`.
- Integration: a first call to an unmapped tool → `ASK` (not DENY), and a subsequent
  unrelated call in the same turn is not denied because of it. `Covers AE4.`
- Edge case: an unmapped tool call does not add any leg to the turn's accumulated set
  (assert via leg-state read, mirroring `test_spawn_does_not_accumulate_any_legs`).

**Verification:** A first unmapped-tool call asks once and never bricks subsequent calls.

---

- U5. **Document the KTD reversals and update config comments**

**Goal:** Record the two deliberate security-stance reversals so they are reviewable, and
fix the now-inaccurate shipped comments.

**Requirements:** Supports R1, R2, R7 (documentation of the stance change; origin Key
Decision "Documented KTD reversals — deliberate")

**Dependencies:** U1, U2, U4

**Files:**
- Modify: `trust/README.md` (KTD notes: (i) leg accumulation is now turn-scoped, not
  session-monotonic; (ii) unknown tool now contributes zero legs + ASK, not all-three →
  DENY; record both as intentional)
- Modify: `trust/config/connector-legs.toml` (header comment currently says an unmapped
  tool "fails conservative — treated as touching all three legs"; correct it to
  zero-legs + ASK and reference the gate-hardening decision)
- Modify: `trust/tests/smoke_substrate.md` if it asserts the old unknown-tool behavior

**Approach:** Documentation only; no behavior. Keep the KTD numbering/convention already
used in `trust/README.md`.

**Test scenarios:** Test expectation: none — documentation/config-comment changes with no
behavioral effect. (The behavior is covered by U1–U4 tests.)

**Verification:** `trust/README.md` and `connector-legs.toml` describe the shipped
behavior; no doc claims the old all-three-legs unknown default.

---

## System-Wide Impact

- **Interaction graph:** Touches the gate's central decision path (`gate()` steps 5–6),
  the `UserPromptSubmit` hook (`mark_turn`), provenance read/write, classification, and
  audit redaction. All within the `trust/` package; no callers outside it change.
- **Error propagation:** Provenance/`turn_id` read failures must continue to fail closed
  (missing → session-scoped accumulation; never an open/empty-legs-by-error that would
  weaken the gate). Mirrors the existing `read_provenance() → {}` fail-closed contract.
- **State lifecycle risks:** Re-keying leaves stale per-turn leg-state files (deferred
  cleanup). The `turn_id` counter is per provenance file; a single always-on session is
  the design target.
- **API surface parity:** `GateResult.to_permission_output` is unchanged — owner trifecta
  emits a standard `ask` decision; the new signal is the audit marker, not a new
  permission shape. No change to the Claude Code hook contract.
- **Integration coverage:** The cross-turn reset and the owner-vs-non-owner completion
  branch are integration behaviors (`test_gate.py`) that per-module unit tests alone won't
  prove — both are enumerated above.
- **Unchanged invariants:** The threat property (untrusted content cannot complete a send
  without an informed human decision) holds — preserved at turn granularity by U1/U2; the
  sticky-untrusted-within-turn behavior, protected-config and trust-state write guards,
  always-gate, and tier resolution are all unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Turn-scoping silently splits a multi-turn task's protection, weakening Rule-of-Two | Accepted by design: a turn is the autonomous unit; the human re-enters at each prompt and every send is still individually gated. Documented in the origin brief and U5. |
| Owner-tier ASK becomes a rubber-stamp on a poisoned send | (c) keeps the human in the loop with an explicit leg-naming warning; R6 audit marker makes repeated approvals reviewable; active escalation deferred, not foreclosed. |
| `turn_id` read failure opens the gate | Fail-closed fallback to session-scoped accumulation (over-accumulate), never to empty legs; mirrors existing provenance contract. Explicit edge-case test. |
| Unknown→zero-legs lets a genuinely-sending unmapped tool slip the send leg | Accepted, documented KTD reversal: known send tools are explicitly mapped; the unmapped call still ASKs (human sees it); always-gate still covers mapped send classes. |
| Existing AE1 same-session test breaks under turn-scoping | The missing-`turn_id` fallback preserves same-session accumulation; AE1 fixture sets no `turn_id`, so it continues to pass. |

---

## Documentation / Operational Notes

- `trust/README.md` KTD reversals (U5) are the operational record of the security-stance change.
- No migration: leg-state files are ephemeral runtime state; on deploy the new keying
  simply starts fresh. Hook/config activation follows the existing rules (settings.json
  change needs a session restart; Python/TOML picked up per call) — `mark_turn` is already
  registered, so no settings.json change is required for this fix.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-27-trifecta-lifecycle-and-unknown-tool-requirements.md](docs/brainstorms/2026-06-27-trifecta-lifecycle-and-unknown-tool-requirements.md)
- Related code: `trust/gate.py`, `trust/legstate.py`, `trust/mark_turn.py`,
  `trust/provenance.py`, `trust/classify.py`, `trust/redact.py`, `trust/policy.py`
- Related issues: #7, #6 (item #6.1)
