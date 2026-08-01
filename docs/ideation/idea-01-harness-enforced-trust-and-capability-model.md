# Harness-enforced trust & capability model

*Top-ranked direction from ideation on an always-on AI chief of staff built on Claude Code (2026-06-22). Confidence \~88% · Complexity: High · Axis: Connectors & permissions.*

## The idea

Make security a property of the **grant mechanism**, not of model behavior.

* **Trust by channel, never by content.** Every inbound trigger (email, calendar invite, DM, webhook) is assigned a trust tier from its *authenticated channel identity*. A message that says "this is urgent, override your restrictions" gets the trust level of its transport layer — full stop.

* **A capability broker holds the keys, not the model.** Connectors live behind a broker process that issues short-lived, narrowly-scoped capability tokens *per task*. The LLM never holds long-lived API keys; it must *request* a capability ("read calendar, next 7 days"; "draft — not send — an email to X").

* **Rule of Two, enforced at grant time.** The broker refuses any single task that would simultaneously combine the three "lethal trifecta" legs — private-data read + untrusted-content exposure + external send — and splits it into chained sub-tasks with a human-approval gate between them.

* **Built on Claude Code primitives.** PreToolUse hooks (deny → ask → allow, enforced by the harness, not the model) and MCP allowlists keyed to `mcp__<server>__*` are the enforcement surface.

## Why this is the top pick

A personal chief-of-staff structurally holds all three legs of the lethal trifecta *by design*: it reads private financial/health/schedule data, processes untrusted inbound content, and can communicate externally. It is the only direction in the set whose failure mode loses the user's **entire** personal knowledge base to a single poisoned input.

Putting the defense at the harness/broker boundary — outside the model — means an injection can't argue its way past it, and every connector added later inherits the protection for free. This is also exactly the reason the concept brief chooses Claude Code over open-source agent infrastructure: it wants Anthropic's built-in controls, approval/permission infrastructure, and auth rather than re-implementing them.

## Basis

* **External:** Simon Willison's "lethal trifecta"; Meta's "Agents Rule of Two" (allow ≤2 of the 3 legs per task); OWASP Agentic Top-10 2026 ranks prompt injection #1; the EchoLeak M365 Copilot zero-click exfiltration (CVE-2025-32711) is a real precedent for the exact attack vector.

* **External:** coleam00/second-brain-starter's "Python CLI wrapper so the LLM never holds API keys" is the same insight; static MCP allowlists alone can't enforce the *per-task* Rule of Two — that dynamic, scoped grant is the non-obvious move.

## Known tradeoffs

* Highest implementation burden in the idea set.

* Per-task capability scoping adds latency and design complexity.

* Task-splitting can make some legitimately-combined workflows feel bureaucratic.

* Requires defining a trust taxonomy (which channels map to which tiers) up front.

## Standing trust policy

Make the gate above opt-out, not mandatory. A declarative, user-owned policy file (versioned in the repo, viewable like every other document) records which action classes the user has pre-authorized, so the broker consults it *before* deciding whether to gate an action.

* **Three review tiers per action class:** `auto` (act and log, no gate) · `notify` (act, then report, with an undo window) · `review` (gate before acting). Anything unmatched defaults to `review` — fail-safe.

* **Example rules:** `payees.acme: auto ≤ $500`; `email replies to known contacts: auto`; `new payee OR amount > $2,000: review`; `brokerage account: always review`.

* **Trust is earned and recorded, not global.** An action class graduates `review → notify → auto` after N clean approvals and is demoted on a single bad outcome; every grant and demotion is an entry in the activity ledger, so the trust history stays auditable.

* **The policy is itself Rule-of-Two protected:** it can only be edited by the user directly, or by a task with no untrusted-input leg — otherwise a poisoned message could grant itself `auto`.

This is the pre-authorized tier of a maker-checker model, and it is what keeps the harness-enforced gate from being exhausting to live with day to day. *(Addresses open question 5.)*

## Open questions to brainstorm next

1. What is the concrete trust-tier taxonomy across the user's actual channels?
2. What does the broker look like as a process — a local daemon, an MCP server, a hook shim?
3. How are "chained sub-tasks with an approval gate" represented so they're legible on mobile remote control?
4. Which actions are high-blast-radius enough to always require the gate, regardless of trifecta count?
5. What's the mechanism to record and implement trust, where a user decides a set of actions are acceptable with single or no review?
