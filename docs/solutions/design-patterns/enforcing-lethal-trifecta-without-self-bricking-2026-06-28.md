---
title: Enforcing the lethal trifecta without self-bricking the assistant
date: 2026-06-28
category: design-patterns
module: trust gate / lethal-trifecta enforcement
problem_type: design_pattern
component: assistant
severity: high
related_components:
  - development_workflow
  - tooling
  - testing_framework
applies_when:
  - Designing a PreToolUse trust gate that blocks the lethal trifecta
  - Choosing leg-state scope (per-turn vs per-session) for trifecta tracking
  - Classifying unknown or unrecognized tools at the gate
  - Choosing a fail-closed default that could lock out the owner
tags:
  - lethal-trifecta
  - trust-gate
  - fail-closed
  - ai-sandwich
  - leg-state
  - assistant
  - security
---

# Enforcing the lethal trifecta without self-bricking the assistant

## Context

The "lethal trifecta" is the combination of three capabilities that together enable data exfiltration via prompt injection: **reading private data**, **ingesting untrusted content**, and **sending to an external destination**. Any agent that holds all three in a single unit of work can be steered by attacker-controlled text it just read into shipping your private data out. A Rule-of-Two gate refuses any task that holds all three legs at once — the agent must give up at least one leg to proceed.

The trap is in the *lifecycle* of that accounting. The naive implementation accumulates legs over the whole **process lifetime** and never resets. On an always-on assistant this is fatal: the first approved external send plus any later untrusted read permanently lights all three legs, and every subsequent tool call — forever, across unrelated tasks — is hard-denied until you restart the process. The safety mechanism becomes a self-inflicted denial-of-service. The gate is "active," looks correctly configured, and bricks the box.

## Guidance

**Scope leg-state to the turn, not the session.** Reset the accumulated leg set at the `UserPromptSubmit` boundary (each new human prompt). Within a turn, accumulation still works exactly as intended — a read-untrusted-then-act injection sequence inside one turn trips the gate. Across turns, the human re-enters the loop simply by typing the next prompt. This is the "AI sandwich": the human is the bread on both ends, and a fresh turn is a fresh, human-initiated slice. You do not need to remember legs across turns because the human's re-prompt *is* the reset authority.

**At the owner tier, a completed in-turn trifecta is a loud ASK, not a hard DENY.** When the owner is present and individually approving each privileged action, a hard refusal removes the human at the exact moment judgment matters most. Instead, escalate to a leg-naming ASK that tells the owner all three legs are now lit and that untrusted content they read may be steering the action. Non-owner and automation tiers — where there is no human *on the loop* — keep the hard DENY.

**Decouple classification from decision for unknown tools.** "Unknown tool → assume all three legs → deny" combined with sticky accumulation will brick the session on the harness's own tool loader (e.g. a deferred-tool discovery call). Split the two concerns: *classification* answers "what legs does this tool carry?" and an unknown tool carries **zero** legs (it does not poison accumulation); *decision* answers "what do we do when we don't recognize it?" and the answer is **ask the human**, not deny. Safe-by-default becomes "unrecognized → ask," not "assume the worst and self-brick."

**Removing a fail-closed default demands a compensating guard.** Softening unknown-tool handling from deny to ask opens a footgun: an unmapped tool whose action class was promoted to `auto`/`notify` could now run a send silently. The compensating guard: an unmapped tool can never honor an `auto`/`notify` promotion — it is capped at `ask` until it is explicitly classified (mapped, even to zero legs). You map a tool to promote it.

## Why This Matters

This is a class of bug where the security control degrades into a liveness failure. A trifecta gate that bricks on first use is worse than no gate, because it trains operators to disable it. The turn-scoping insight — that the human prompt boundary is a natural, trustworthy reset — is what makes a strict Rule-of-Two gate compatible with a long-running, always-on agent. The unknown-tool sub-lesson generalizes to any allowlist-shaped safety system: conflating "I don't recognize this" with "this is maximally dangerous" turns every new or renamed tool into an outage.

## When to Apply

- Any always-on or long-lived agent that holds private-data, untrusted-content, and external-send capabilities.
- Any safety gate that **accumulates** state across calls (taint tracking, capability unions, rate budgets) — decide its reset boundary deliberately; process-lifetime is almost never right.
- Any allowlist/classifier where "unrecognized input" needs a decision distinct from "known-dangerous input."
- Any time you soften a fail-closed default — pair it with a compensating cap so you don't open a silent bypass.

## Examples

Turn-scoped accumulation: the per-turn provenance writer stamps an incrementing `turn_id`, and the gate keys leg-state on `session:turn` so a new turn starts clean. A missing `turn_id` falls back to session-lifetime scoping — conservative (over-accumulate, never under-accumulate), preserving the fail-closed contract:

```python
# trust/mark_turn.py — UserPromptSubmit hook, fires once per turn
prior = read_provenance().get("turn_id")
turn_id = prior + 1 if isinstance(prior, int) else 1
write_provenance({"kind": kind, "turn_id": turn_id})
```

```python
# trust/gate.py — leg accumulation scoped to the turn, not the session
turn_id = prov.get("turn_id")
leg_scope = f"{session_id}:{turn_id}" if isinstance(turn_id, int) else session_id
acc = legstate.accumulate(leg_scope, this_legs, cfg.state_dir)
```

Owner-tier ASK vs. hard DENY on a completed trifecta:

```python
# trust/gate.py — Rule of Two against accumulated legs
if ALL_LEGS <= acc:
    if tier == Tier.OWNER:
        return finish(GateResult(
            Decision.ASK,
            "this turn now holds all three trifecta legs (private read + untrusted "
            "content + external send). Untrusted content you've read may be steering "
            "this action — approve only if you recognize and intend it.",
            tier=tier, legs=acc, action_class=ac),
            trifecta_warning=True)
    return finish(GateResult(
        Decision.DENY,
        "this task has accumulated all three trifecta legs ...; decompose into a "
        "no-untrusted-input privileged step and re-request after approval",
        tier=tier, legs=acc, action_class=ac))
```

Unknown tool → zero legs (classification) → ask (decision):

```python
# trust/classify.py
rule = _match_rule(tool_name or "", config)
if rule is None:
    # Unknown tool — contribute NO legs (don't poison accumulation). The gate still
    # gates it: no legs → standing policy → unmatched-class default `review` → ASK.
    return set()
```

Compensating guard — an unmapped tool can't be auto/notify-promoted:

```python
# trust/gate.py
if ptier in (policy.AUTO, policy.NOTIFY) and not classify.is_mapped(tool_name, cfg.legs):
    return finish(GateResult(
        Decision.ASK,
        f"unmapped tool '{tool_name}' is set to policy:{ptier} but has no "
        "connector-legs classification — gating (ask) until it is classified",
        tier=tier, legs=acc, action_class=ac))
```

The untrusted-content leg is intentionally sticky *within* a turn (a union that never removes), so reading a poisoned document early and sending late in the same turn still trips — turn-scoping resets it, but only at the human boundary.

## Related

- [Running Claude Code always-on](../architecture-patterns/running-claude-code-always-on.md) — every bug in this cluster was "service active / looks fine" but silently broken, invisible to unit tests; only live smoke testing caught the self-brick and the loader-brick.
- [Connector classification as security](../best-practices/connector-classification-as-security-2026-06-28.md) — the leg map that `classify()` consults; a mislabel there feeds wrong legs into this accounting.
- Origin: issue #7 (the discovered self-brick), PR #10 (implementation), `docs/brainstorms/2026-06-27-trifecta-lifecycle-and-unknown-tool-requirements.md`, `trust/README.md` (KTD reversals).
- Code: `trust/legstate.py`, `trust/gate.py`, `trust/mark_turn.py`, `trust/classify.py`.
