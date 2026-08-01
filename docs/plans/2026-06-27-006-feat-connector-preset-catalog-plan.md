---
title: "feat: Connector preset catalog (18 connectors)"
type: feat
status: completed
date: 2026-06-27
origin: (internal tracking issue — connector rubric + 18-row table)
---

# feat: Connector Preset Catalog (18 connectors)

## Overview

Ship conservative, validated trifecta-leg classifications for 18 widely-used connectors
so a new adopter inherits correct gating instead of today's three-connector set
(Gmail/Calendar/Drive). Managed (claude.ai-hosted) connectors ship **active** with
live-confirmed `mcp__claude_ai_<Server>__*` tool names; directory connectors the adopter
adds ship **commented-out** with best-known name patterns plus a "validate via `/mcp`"
note, so an unused preset is never a silent trust grant. This is the MVP half of #6.4
(classification correctness + validation); the rest of #6 stays deferred.

---

## Problem Frame

`connector-legs.toml` is the security classification a new adopter inherits — it declares
which trifecta legs each tool touches, and a mislabel is a real hole (an `external_send`
tool tagged as a mere read becomes an un-gated exfiltration path). Today it covers only
Gmail, Calendar, Drive; everything else falls to the unmapped default (now zero-legs →
ASK after PR #10, so usable but unclassified — and, per PR #10's guard, not promotable to
auto until classified). Early adopters are software-trade, so the catalog targets the
connectors they actually use. The full rubric and per-connector table live in the origin
(see origin: (internal tracking issue — connector rubric + 18-row table)).

---

## Requirements Trace

- R1. Ship leg classifications for 18 connectors per the #9 rubric: external read =
  `private_read` (+ `ingests_untrusted` where the content is others'); visible mutation =
  `external_send`; destructive / irreversible / money ops = hard-gated.
- R2. Managed connectors (Gmail, Google Calendar, Google Drive, Todoist, Dropbox) ship
  **active** with live-confirmed tool names.
- R3. Directory connectors (Slack, Discord, Calendly, Asana, ClickUp, monday.com, GitHub,
  GitLab, Linear, Atlassian, Sentry, Notion, Figma) ship **commented-out** with
  best-known name patterns and a `/mcp`-validation note; enabling is per-connector.
- R4. Hard-gated ops (delete, merge, share-link, money) require approval regardless of
  tier or any policy promotion.
- R5. A classification assertion verifies the shipped (active) presets resolve to the
  expected legs/decision — not merely that the hook fires (#6.4's core ask).
- R6. Setup docs explain enabling a directory preset and validating its names via `/mcp`.

---

## Scope Boundaries

- Not changing the gate engine, classify/legstate/policy logic (PR #10 just landed it).
- Directory-connector tool names are **not** verified against a live instance here — they
  ship commented with best-known patterns; live validation is the adopter's `/mcp` step
  and a `smoke_substrate.md` checklist row. (No live instance available in this repo.)
- Not promoting any connector to `auto`/`notify` — every preset stays at the safe
  `review` → ASK default; promotion is the adopter's per-instance choice.
- Rest of #6 (Bash containment #6.2, protected-config authority #6.3) — deferred.
- Finance connectors with money movement (Stripe/PayPal/Xero) — out of this set;
  if added later they ship fully hard-gated.

---

## Context & Research

### Relevant Code and Patterns

- `trust/config/connector-legs.toml` — the pattern to extend: a wildcard read rule plus
  more-specific `external_send` overrides (the Google Calendar block is the exemplar:
  `mcp__claude_ai_Google_Calendar__*` → `private_read`, then `…__create_event` /
  `…__update_event` / `…__delete_event` / `…__respond_to_event` → `external_send`).
  Longest-literal-prefix wins (`classify._match_rule`).
- `trust/config/always-gate.toml` — `classes = [...]` are action classes always gated
  (already includes `money_movement`, `delete`, `grant_connector_scope`,
  `edit_trust_config`); `recipient_gated_classes = ["send_email","send_message"]` +
  `recipient_fields` drive new-recipient gating.
- `trust/config/policy.toml` — `[classes_of]` maps a tool name → a coarse action class;
  unmatched classes default to `review`. This is how a specific destructive tool is bound
  to a hard-gate class.
- `trust/classify.py` — `classify()` (legs) and `is_mapped()` (PR #10 guard: an unmapped
  tool can't be auto/notify-promoted). The catalog makes tools *mapped*, restoring
  promote-ability for the adopter.
- `trust/tests/test_units.py::TestClassify` — the place to assert shipped legs per
  connector; `trust/tests/test_gate.py` — for hard-gate decision assertions.
- `trust/tests/smoke_substrate.md` — the manual live-runtime checklist (where dir-name
  validation belongs).

### Managed-connector tool surfaces (confirmed from this session's tool list)

These drive the **active** rules:

- **Gmail** `mcp__claude_ai_Gmail__*`: read/draft/label only (`search_threads`, `get_thread`,
  `list_*`, `create_draft`, `*_label`, `apply_sensitive_*`). **No send tool** → keep the
  existing `private_read` + `ingests_untrusted`, no `external_send`. (Already shipped;
  no change.)
- **Google Calendar** `mcp__claude_ai_Google_Calendar__*`: already complete (read wildcard
  + create/update/delete/respond → `external_send`). Add a hard-gate on `delete_event`.
- **Google Drive** `mcp__claude_ai_Google_Drive__*`: read wildcard + untrusted already
  shipped; add `external_send` overrides for `create_file` and `copy_file`. (Surface has
  no delete/share tool → no hard-gate.)
- **Todoist** `mcp__claude_ai_Todoist__*` (new): read wildcard `private_read`; mutation
  verbs (`add-*`, `update-*`, `complete-*`, `uncomplete-*`, `manage-assignments`,
  `add-comments`, `add-reminders`, `reschedule-tasks`, `project-move`, `reorder-objects`,
  `link-goal-tasks`, `delete-object`) → `external_send` (conservative: collaborator-visible).
- **Dropbox** `mcp__claude_ai_DropboxMCP__*` (new): read wildcard `private_read` +
  `ingests_untrusted`; `external_send` overrides for `create_file`, `create_folder`,
  `copy`, `move`, `restore_file_revision`, `restore_folder`, `create_file_request`;
  hard-gate `delete` and `create_shared_link`.

### Directory connectors (ship commented; best-known patterns, validate via `/mcp`)

Slack, Discord (chat); Calendly (sched); Asana, ClickUp, monday.com (tasks); GitHub,
GitLab, Linear, Atlassian/Jira+Confluence, Sentry (eng); Notion, Figma (docs). Each: a
read wildcard (`private_read` + `ingests_untrusted` — these read others' content),
`external_send` overrides for post/comment/create/update, and hard-gate mappings for
destructive/irreversible ops (GitHub/GitLab `merge`, repo/branch `delete`; Notion/Asana/
ClickUp/monday/Linear `delete`; chat new-recipient via `send_message`).

### External References

- None fetched. Directory-connector existence confirmed earlier via web search
  (Anthropic's 400+ connector directory); exact tool names are instance-specific and
  validated at adopter setup, not here.

---

## Key Technical Decisions

- **Hard-gate via the existing action-class mechanism, no code change.** Map a destructive
  tool → a coarse class in `policy.toml [classes_of]` and ensure that class is in
  `always-gate.toml`'s `classes`. Reuse `delete` and `money_movement`; add `merge`,
  `share_link`, and `publish` as new always-gate classes for the catalog. Rejected
  extending `always_gate.py` to match tool-name patterns directly (Option B): cleaner
  per-connector config, but it's an engine change right after PR #10, and the existing
  mechanism is sufficient for the handful of active hard-gates. Note Option B as a
  possible future simplification if the classes_of plumbing proves unwieldy.
- **Active vs commented split = the trust boundary.** Only the 5 managed connectors whose
  names are confirmable here ship active. Everything else is commented so the adopter
  consciously enables (and validates names for) each one — no silent trust grant.
- **Chat recipient-gating needs a `channel` field.** Slack/Discord post to a *channel*,
  not a `to:`. Add `channel` to `always-gate.toml`'s `recipient_fields` and map chat post
  tools to `send_message` so new-channel/new-recipient posting is gated.
- **Classification assertion = executable unit tests over the shipped active config**,
  plus a manual `smoke_substrate.md` checklist row for the commented dir connectors
  (which can only be validated against a live instance). This satisfies #6.4's "assert
  correct classification, not just hook-fires" for everything we can assert offline.

---

## Open Questions

### Resolved During Planning

- How to express hard-gated ops → action-class mapping + always-gate classes (above).
- Which connectors ship active → the 5 managed ones with confirmed names.
- Where the classification assertion lives → `test_units.py` (legs) + `test_gate.py`
  (hard-gate decisions) for active connectors; `smoke_substrate.md` for dir validation.

### Deferred to Implementation

- The exact directory-connector tool-name patterns (best-effort, commented; adopter
  validates via `/mcp`). Not knowable offline.
- Whether Todoist collaborator-comment reads warrant `ingests_untrusted` (the #9 table
  says `private_read` only; implementer may add untrusted on `find-comments`/`fetch` if it
  reads cleanly — note inline, don't over-engineer).

---

## Implementation Units

- U1. **Active managed-connector leg rules**

**Goal:** Extend `connector-legs.toml` so all 5 managed connectors are classified per the
rubric (R1, R2).

**Files:**
- Modify: `trust/config/connector-legs.toml`
- Test: `trust/tests/test_units.py`

**Approach:**
- Google Drive: add `external_send` overrides for `…__create_file`, `…__copy_file`.
- Todoist: new block — read wildcard `private_read`; `external_send` overrides for the
  mutation verbs listed in Context. Use prefix wildcards (`…__add-*`, `…__update-*`, etc.).
- Dropbox: new block — read wildcard `private_read` + `ingests_untrusted`; `external_send`
  overrides for create/copy/move/restore/file-request. (Hard-gate of delete/share-link is
  U2.)
- Gmail / Calendar: already correct — leave (Calendar's delete hard-gate is U2).
- Keep the existing comment style; each block headed by the connector name + one line on
  its leg rationale.

**Patterns to follow:** the Google Calendar wildcard-plus-overrides block already in the
file.

**Test scenarios:**
- Happy path: `classify("mcp__claude_ai_Google_Drive__create_file", …)` →
  `{private_read, external_send}`; `…__read_file_content` → `{private_read, untrusted}`.
- Happy path: `classify("mcp__claude_ai_Todoist__add-tasks", …)` includes `external_send`;
  `…__find-tasks` → `{private_read}` only.
- Happy path: `classify("mcp__claude_ai_DropboxMCP__move", …)` includes `external_send`;
  `…__list_folder` → `{private_read, untrusted}`.
- Edge case: a Dropbox read tool does **not** carry `external_send` (longest-prefix
  override doesn't bleed onto reads).

**Verification:** `TestClassify` covers one read + one mutation per managed connector and
passes; existing Gmail/Calendar/Drive tests still pass.

---

- U2. **Hard-gate wiring for destructive managed ops**

**Goal:** Make the irreversible managed ops always-gated regardless of tier or policy
promotion (R4).

**Files:**
- Modify: `trust/config/always-gate.toml` (add `share_link` to `classes`; add `merge`,
  `publish` for the dir catalog; add `channel` to `recipient_fields`)
- Modify: `trust/config/policy.toml` (`[classes_of]`: `…Google_Calendar__delete_event` →
  `delete`; `…DropboxMCP__delete` → `delete`; `…DropboxMCP__create_shared_link` →
  `share_link`)
- Test: `trust/tests/test_gate.py`

**Approach:**
- Bind each destructive managed tool to a hard-gate class via `classes_of`; ensure the
  class is in `always-gate.toml`'s `classes`. `delete` already present; add `share_link`.
- These tools are already leg-mapped (U1 / existing), so PR #10's unmapped-guard is
  satisfied and the hard-gate is the binding constraint.

**Dependencies:** U1

**Test scenarios:**
- Happy path: a `delete_event` / Dropbox `delete` call → `ASK` with an always-gate reason,
  even when provenance is owner and even if policy were promoted (assert via a fixture
  that promotes the class to `auto` — always-gate still wins).
- Happy path: Dropbox `create_shared_link` → `ASK` via the `share_link` always-gate class.
- Edge case: a non-destructive Dropbox mutation (`move`) is **not** caught by the
  always-gate (it's `external_send` → normal review ASK, not the hard-gate class) — proves
  the hard-gate is scoped to the destructive ops, not all mutations.

**Verification:** hard-gated managed ops ASK regardless of policy; non-destructive
mutations follow ordinary policy.

---

- U3. **Directory-connector commented catalog**

**Goal:** Ship the 13 directory connectors as a commented, per-connector-enableable
catalog with best-known patterns and validation notes (R3).

**Files:**
- Modify: `trust/config/connector-legs.toml` (commented blocks)
- Modify: `trust/config/policy.toml` and `trust/config/always-gate.toml` (commented
  hard-gate / recipient-gate examples per connector)

**Approach:**
- One commented block per connector: a read wildcard (`private_read` + `ingests_untrusted`),
  `external_send` overrides for the visible mutations, and commented hard-gate mappings
  (GitHub/GitLab `merge` + repo/branch `delete`; Notion/Asana/ClickUp/monday/Linear
  `delete`; chat `send_message` recipient-gating with the `channel` field).
- Each block opens with: enable by uncommenting **and** validating the tool names against
  your instance via `/mcp`; names are best-known, not confirmed.
- Group by the #9 categories (chat / sched / tasks / eng / docs) for legibility.

**Dependencies:** U2 (so the always-gate classes the comments reference exist)

**Test scenarios:** Test expectation: none — commented config has no runtime effect.
(Coverage is the U1/U2 active tests plus the U4 doc/checklist validation step.)

**Verification:** uncommenting a block + adding the server yields the documented legs (the
adopter confirms via the `smoke_substrate.md` row from U4); no active rule is added by
this unit (suite unchanged).

---

- U4. **Classification assertion + adopter docs**

**Goal:** Lock the #6.4 "assert correct classification" requirement and document enabling
+ validating presets (R5, R6).

**Files:**
- Modify: `trust/tests/smoke_substrate.md` (add a per-connector *classification* check:
  for each enabled connector, exercise a read and a mutation and confirm the gate logs the
  expected legs/decision — not just that the hook fired; a row for dir-name `/mcp`
  validation)
- Modify: `trust/README.md` (a "Connector presets" subsection: active vs commented,
  enabling a dir connector, the `/mcp` name-validation step, and that hard-gated ops can't
  be promoted)
- Modify: `docs/SETUP.md` (point the connector step at the catalog + validation)

**Dependencies:** U1, U2, U3

**Test scenarios:** Test expectation: none — docs + manual-checklist changes. The
executable classification assertions live in U1/U2.

**Verification:** README/SETUP describe per-connector enablement and `/mcp` validation;
`smoke_substrate.md` asserts classification correctness for enabled connectors.

---

## System-Wide Impact

- **Interaction graph:** config-only for the engine; `classify` and `always_gate` read the
  new rules/classes, no code path changes. The new always-gate classes (`share_link`,
  `merge`, `publish`) only bite when a `classes_of` mapping points at them.
- **State lifecycle risks:** none — static config.
- **API surface parity:** the active set must use the exact `mcp__claude_ai_<Server>__*`
  names; a typo silently leaves a connector unmapped (now zero-legs → ASK, not a deny, so
  fail-safe-ish but mis-classified). The U1 tests guard the active names.
- **Unchanged invariants:** the gate engine, the turn-scoping / owner-ASK / unmapped-guard
  behavior from PR #10, and the safe-by-default `review` posture all stay; this only adds
  classifications and hard-gate bindings.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A mutation mis-tagged as read-only → un-gated exfiltration path | Conservative rubric (visible mutation → `external_send`); per-connector U1 tests assert mutations carry the send leg; managed names confirmed from the live tool list |
| Directory-connector name patterns are guesses | They ship **commented**; enabling requires the adopter to validate via `/mcp` (README + `smoke_substrate.md` row). An unvalidated preset is inert. |
| `classes_of` plumbing for hard-gates is verbose/error-prone across 18 connectors | Only the few **active** managed hard-gates are wired now; dir hard-gates are commented examples. Option B (tool-pattern always-gate) noted as a future simplification. |
| Todoist over-marking private task edits as `external_send` | Accepted as conservative; the cost is an extra ASK on own-task edits, not a security hole. Adopter can promote in `policy.local.toml`. |

---

## Documentation / Operational Notes

- Adopters enabling a dir connector touch up to three files (legs, policy classes_of,
  always-gate) — the README subsection must make that sequence explicit, or the hard-gate
  silently won't apply.

---

## Sources & References

- **Origin:** internal tracking issue (rubric + 18-row table)
- Related code: `trust/config/connector-legs.toml`, `trust/config/always-gate.toml`,
  `trust/config/policy.toml`, `trust/classify.py`, `trust/always_gate.py`
- Related: #6 (#6.4 is this; #6.2/#6.3 deferred), PR #10 (unmapped-tool guard this builds on)
