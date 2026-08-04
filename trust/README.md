# buzai trust & capability model

A PreToolUse-hook gate for a self-hosted Claude Code "chief of staff". Every tool
call is assigned a trust tier from its turn's authenticated provenance (never its
content), classified against the three "lethal trifecta" legs, accumulated across
the turn, and allowed / asked / denied against the Rule of Two, a standing trust
policy, and an always-gate list — with a redacted, tamper-evident audit entry on
every decision.

## Dependencies

None beyond Python 3.11+ (uses stdlib `tomllib` for config and `unittest` for
tests). Config is TOML rather than the YAML the plan sketched — a deliberate
zero-dependency choice for a self-host template.

Rather than depend on the host's system python (unknown version, a common adopter
footgun), the deploy path pins a self-contained interpreter via `uv` as a `.venv`
at the workspace root, and the hook runs through `trust/bin/gate` which execs that
`.venv/bin/python`. See `docs/SETUP.md` step 5 for the one-command setup. Working on
the package directly, you can use any Python 3.11+ on your PATH.

## Install (in your assistant workspace)

1. **Verify the substrate first.** Run `trust/tests/smoke_substrate.md` in your
   real runtime. It confirms PreToolUse hooks fire on your managed connectors and
   on the subagent (`Agent`/`Task`) spawn (RB2), and exercises the provenance writer.
   Do not rely on the gate for any source it doesn't cover.
2. **Register the hooks.** Merge `trust/settings.example.json` into your workspace's
   `.claude/settings.json` — *merge*, do not overwrite. It registers two hooks: the
   `PreToolUse` gate (`trust/bin/gate`) and the `UserPromptSubmit` provenance writer
   (`trust/bin/mark-turn`, below).
3. **Provenance is written for you (single principal).** The `UserPromptSubmit` hook
   runs `trust/bin/mark-turn` once per turn, writing `<workspace>/.buzai/provenance.json`
   = `{"kind":"owner","turn_id":N}` before that turn's tool calls — so the gate resolves
   the owner tier with no config (the `turn_id` counter is what scopes leg-state to the
   turn; see "How it decides" step 2). The shipped `tiers.toml` already maps `owner → owner`, so no
   `tiers.local.toml` edit is needed for the owner case. A missing/unreadable file
   still fails closed to untrusted-external. The gate denies any tool write under
   `.buzai/` (provenance + legstate), so a task cannot forge its own owner tier or
   reset its accumulated trifecta legs.
4. **Add other channels later (optional).** When you introduce automation/known-contact
   sources, those injectors write their own provenance (a stable `channel` id, or
   `BUZAI_TURN_KIND=automation` reusing `mark-turn`) and you map them in
   `trust/config/tiers.local.toml` (see `tiers.local.toml.example`).

## How it decides (in order)

1. Resolve tier from provenance (metadata only).
2. Classify this call's legs; accumulate across the **turn** (leg-state is keyed on the provenance `turn_id` and resets each turn; untrusted-content stays sticky *within* the turn).
3. Edit to a protected config (`policy` / `always-gate` / `known-recipients` / `tiers` / `connector-legs`)? → allowed only from owner tier or a task with no untrusted-content leg.
4. On the always-gate list (incl. sends to a new recipient)? → ask.
5. All three legs accumulated this turn? → at **owner tier**, a loud ASK naming the three legs (the owner is the present human backstop, individually gating every send); at **non-owner tiers**, deny with a decompose-and-reapprove reason. Owner approvals here carry a `trifecta_warning` audit marker.
6. Otherwise the standing policy tier: `review` → ask, `notify` → allow then report with undo, `auto` → allow.

### Leg-state lifecycle & the unknown-tool default — two deliberate reversals

Two early key decisions were reversed in a later gate-hardening pass, because both
made the always-on session **self-brick**:

- **Leg accumulation is turn-scoped, not session-monotonic.** Originally legs unioned
  for the whole process lifetime and never reset, so the first approved send + any later
  untrusted read permanently lit all three legs and denied everything until restart. Now
  the gate keys leg-state on `session_id:turn_id` (the `turn_id` is stamped each
  `UserPromptSubmit` by `mark-turn`), so a turn starts fresh. A missing `turn_id` falls
  back to session-scoping (conservative). The threat property is preserved: a
  read-then-act sequence *within one turn* still accumulates and trips Rule-of-Two — and
  across turns the owner re-enters at each prompt and every send is still individually
  gated.
- **An unknown/unmapped tool contributes zero legs, not all three.** Originally an
  unmapped tool failed conservative to all three legs → Rule-of-Two deny → with
  stickiness, one call to any unrecognized tool bricked the session. Now an unmapped tool
  carries no legs and is gated via the standing policy's default `review` → ask (so it
  still requires owner approval; nothing auto-runs). Trade-off: a genuinely-sending
  *unmapped* tool no longer auto-trips the `external_send` leg, so map real send tools
  explicitly (the connector preset catalog). Known send/read tools stay mapped.
  **Guard:** an unmapped tool can never run `auto`/`notify` even if its action class is
  promoted in `policy.toml` — the gate caps it at ask until it has a `connector-legs`
  rule. So a "promoted to auto but forgot the leg mapping" config slip can't produce a
  silent, ungated send; the fix is to map the tool (even to zero legs) before promoting it.

Fail-closed is engineered in the shim (`hook.py`): a gate timeout, exception, or
unparseable input maps to **deny** — Claude Code's own default on hook timeout is
to *proceed*, so the gate does not rely on it.

## Subagent containment

PreToolUse fires on the `Agent`/`Task` *spawn*, but Claude Code scopes hooks
per-subagent, so a main-session hook may **not** fire on a subagent's *internal*
tool calls — which would let the assistant delegate a privileged action past the
gate. v1 contains this by listing `Agent` and `Task` on the always-gate
(`always-gate.toml`), so every spawn requires owner approval; an explicit zero-leg
rule in `connector-legs.toml` keeps the spawn from polluting session leg state.
This keeps delegation available but owner-approved per spawn. If you need *unattended*
delegation, add per-connector / MCP-allowlist enforcement that survives delegation
before relying on it (deferred). Confirm your version's subagent-internal hook
behavior with `trust/tests/smoke_substrate.md` (check 2b).

## Built-in tools & the Bash blind spot

The PreToolUse matcher is `*`, so Claude Code's built-in tools pass through the gate
too and are classified in `connector-legs.toml`. They are no longer *denied* when
unmapped (the unknown default is now zero-legs → ask, see the lifecycle note above),
but classifying them is still worth it: it gives reads their correct private-read leg
and `auto` allow (no prompt), and gives writes their explicit zero-leg mapping — rather
than every built-in falling to a generic ask. Shipped mappings: read-only inspection
(`Read`/`Grep`/`Glob`/`LS`/`NotebookRead`) → private-read and `auto` in `policy.toml`
(matching Claude Code's default of not prompting for reads; legs are still
accumulated, so the trifecta still fires on a later completing call); local writes
(`Write`/`Edit`/…) → no legs (dangerous writes are caught by the protected-config and
`.buzai/` trust-state guards regardless); `WebSearch` → untrusted-content.

**Bash is a declared blind spot.** Bash is general-purpose — a single command can
read private data, ingest untrusted content, *and* exfiltrate — so static
leg-classification can't capture it, and per-command approval is unreliable (shell
operations are cryptic, and the only promotion granularity here is the whole `Bash`
class). It is mapped to zero legs so it stays usable (defaults to `review` → ask, like
vanilla Claude Code), but **the gate does not meaningfully constrain Bash**. Real
containment is structural — the dedicated unprivileged user, OS/network-egress limits,
or sandboxing/disabling Bash in a trust-sensitive config — and is a **deferred design
pass**. Do not rely on the gate to stop Bash-based exfiltration.

**Deferred (paired with multi-source provenance): protected-config edit authority.**
Because local writes carry no legs by themselves, `can_edit_protected` now follows R8
literally — owner tier *or* any task with no untrusted-content leg. In the
single-principal model this is moot (every turn is owner). When automation/inbound
provenance is added, decide whether editing a protected trust config should require
the **owner tier outright**, rather than allowing any clean-but-untrusted-tier task —
an untrusted turn whose instruction arrived via the prompt (never tool-read) would
otherwise carry no untrusted-content leg.

## Configuration files (`trust/config/`)

| File | What it controls |
|---|---|
| `tiers.toml` | channel/kind → trust tier (override owner mapping in `tiers.local.toml`) |
| `connector-legs.toml` | tool-name patterns → trifecta legs |
| `policy.toml` | action class → `auto` / `notify` / `review` (unmatched → review) |
| `always-gate.toml` | high-blast-radius classes + recipient-gated send classes |
| `known-recipients.toml` | recipients that don't trip the new-recipient gate |

## Connector presets

`connector-legs.toml` ships a catalog of conservative classifications for widely-used
connectors. Two tiers:

- **Active (managed):** Gmail, Google Calendar, Google Drive, Todoist, Dropbox ship
  with **live-confirmed** `mcp__claude_ai_<Server>__*` names and are enforced as-is.
- **Commented (directory):** Slack, Discord, Calendly, Asana, ClickUp, monday.com,
  GitHub, GitLab, Linear, Atlassian, Sentry, Notion, Figma ship **commented out** with
  best-known name patterns. They are inert until you enable them, so an unused preset is
  never a silent trust grant.

**To enable a directory connector:** add it to your instance, run `/mcp` to read its
**real** tool names, then uncomment its block in `connector-legs.toml` and fix the
`match` patterns to those names. For its hard-gated ops, add the noted `[classes_of]`
entry to `policy.toml` (the `delete` / `merge` / `publish` / `share_link` always-gate
classes already exist). A name that doesn't match your instance silently leaves the tool
unmapped (zero legs → ask, but misclassified) — validate with
`trust/tests/smoke_substrate.md` step 5.

**Classification rubric** (conservative — a mislabel is a security hole): an external or
shared read = `private_read` + `ingests_untrusted`; a mutation others can see =
`external_send`; a destructive/irreversible/money op = **hard-gated** (bound to an
always-gate class, so it asks regardless of tier or any policy promotion). Only your own
private state (own todos, own calendar) reads as `private_read` alone.

## Audit log

Decisions append to `audit/audit.jsonl`, redacted and hash-chained. Each entry
carries a UTC ISO-8601 `ts` *inside* the hashed payload, so the chain fixes both
ordering and wall-clock time and a rewritten timestamp breaks verification.
`trust.audit.verify_chain` detects tampering. Store `audit/` where only the gate
process can write it (not an arbitrary tool call) — set filesystem permissions
accordingly at deploy time.

Redaction is **field-role aware** (`redact.redact_entry`): the fields the gate
itself authors — `tool`, `action_class`, `decision`, `tier`, `legs`, and the
`trifecta_warning` marker on owner-tier trifecta approvals — stay
legible because they *are* the audit signal, `session` is reduced to a stable
hash reference (correlatable, not raw), `reason` keeps its text but scrubs
embedded PII (a new-recipient reason can carry an email → domain only), and
`tool_input` (the untrusted payload) gets the full treatment: credentials,
emails→domain, secret-bearing fields, content→length+hash, paths→basename.
(Applying the payload redactor to the whole entry would hash long managed-connector
tool names like `mcp__claude_ai_Google_Calendar__list_events` as opaque tokens,
making the log unreadable for exactly the connector calls it must record.)

## Emergency bypass

If a gate bug denies everything, disable the hook by removing its entry from
`.claude/settings.json`. Keep the bypass **bounded and audited**: re-enable the
gate as soon as the issue is fixed; the re-enable path requires no owner auth, so a
bad provenance/owner-signal bootstrap can't trap you. (A persistent off-switch is
equivalent to no gate — treat bypass as time-limited.)

## Tests

```bash
# host python (any 3.11+), or swap in .venv/bin/python to match the deploy interpreter
python3 -m unittest trust.tests.test_units trust.tests.test_gate
```
