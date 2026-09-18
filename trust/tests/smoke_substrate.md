# U1 — Substrate verification (run in the live assistant runtime)

The gate's design rests on platform facts about Claude Code hooks that cannot be
verified in this template repo (it has no connectors wired). Run this procedure in
your real always-on assistant workspace — the one with the connectors configured —
**before** trusting the gate, and record the results in the table below. RB2 in
particular can change the architecture if it fails.

## Procedure

1. Register a throwaway logging PreToolUse hook in that workspace's
   `.claude/settings.json`:

   ```json
   {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
     {"type": "command",
      "command": "jq -r '.tool_name' >> /tmp/buzai-hook-fired.log"}]}]}}
   ```

   Then **restart the Claude Code session** — hook config is snapshotted at session
   start, so a hook added mid-session does not fire until you restart. (This is the
   same reason the real gate's `audit/audit.jsonl` stays empty until a post-wiring
   restart.)

2. Exercise one tool from each source and check whether its name lands in the log:
   - each **managed connector** you wired: Gmail, Google Calendar, Google Drive
   - each **local MCP server** you wired: e.g. Sheets, IMAP/SMTP
   - a **built-in** tool: Bash or Read
   - each **directory connector** you enabled from the catalog (Slack, Notion, GitHub,
     …): confirm its real tool names from `/mcp` match the `match` patterns you
     uncommented in `connector-legs.toml` — a mismatched name silently leaves the
     connector unmapped (zero legs → ask, but misclassified).
   - **subagent coverage (RB2)** — two distinct checks:
     - **(2a) the spawn** — invoke the `Agent` (a.k.a. `Task`) tool and confirm the
       hook fires on the spawn call itself. (Expected: yes. This is the containment
       point the gate relies on — see RB2 below.)
     - **(2b) subagent-internal calls** — have the spawned subagent make its own tool
       call (e.g. a Read) and check whether the **main-session** hook fires for *that*
       call. Claude Code scopes hooks per-subagent, so it may NOT. This is the open
       empirical question.

3. Confirm the provenance writer (RB1): submit a normal prompt and confirm the
   `UserPromptSubmit` hook (`trust/bin/mark-turn`) wrote `<workspace>/.buzai/provenance.json`
   = `{"kind":"owner","turn_id":N}` (the `turn_id` counter increments each turn and
   scopes leg-state to the turn). Verify it reads back:
   `.venv/bin/python -c "from trust.provenance import read_provenance; print(read_provenance())"`.
   Delete the file and confirm a missing file yields `{}` (→ untrusted, fail-closed).

4. Confirm the owner-tier signal (RB3): with the writer's `{"kind":"owner"}` in place,
   verify `resolve_tier` returns `OWNER` (the shipped `tiers.toml` `[kinds]` maps
   `owner → owner`, so no `tiers.local.toml` edit is needed), and that an absent file
   resolves to `UNTRUSTED_EXTERNAL`.

5. Confirm **classification correctness**, not just that the hook fired: for each
   enabled connector, exercise a *read* and a *mutation* and confirm the audit entry
   records the **expected legs and decision**, not merely that the gate ran. Read the
   tail of the log after each call:
   `tail -n 1 audit/audit.jsonl | python3 -m json.tool`. Expectations:
   - a **read** (e.g. Gmail `search_threads`, Drive `read_file_content`, Dropbox
     `list_folder`) → `legs` includes `private_read` (and `untrusted_content` for
     mail/files/chat/issues), decision `ask` (or `allow` if you promoted it);
   - a **send/mutation** (e.g. Calendar `create_event`, Todoist `add-tasks`, a Slack
     post) → `legs` includes `external_send`;
   - a **hard-gated op** (Calendar `delete_event`, Dropbox `delete` /
     `create_shared_link`, a GitHub merge) → decision `ask` with an `always-gate`
     reason **even if** you promoted its class to `auto`/`notify`.
   A managed connector's expected legs are pinned by the unit tests
   (`trust/tests/test_units.py::TestClassify`, `test_gate.py::TestConnectorHardGate`);
   this step confirms the same holds against your live tool names.

## Results (fill in)

Run on **2026-09-18**, Claude Code **2.1.275**, instance `buzai-smoke` (fresh install
from `feat/private-versioned-hubs`). Method: a throwaway `PreToolUse` logging hook
(`jq -r '.tool_name'`) registered alongside the gate, exercised from the Claude desktop
app over Remote Control, then compared against `audit/audit.jsonl`. The hook was removed
afterwards. **Control:** the log was confirmed non-empty for tools already accounted for
before any conclusion was drawn from its silence — an empty log cannot distinguish "the
hook did not fire" from "the instrument is broken".

| Source | Hook fired? | Legs/decision correct? (step 5) | Notes |
|---|---|---|---|
| Gmail (managed) | ☑ | ☑ | `search_threads`, `get_message` → `private_read` + `untrusted_content`, ask |
| Google Calendar (managed) | ☑ | ☑ | `list_events` → `private_read` + `untrusted_content`, ask; `create_event` → `external_send`, ask. Delete not exercised |
| Google Drive (managed) | ☐ | ☐ | connected, NOT exercised |
| Todoist (managed) | ☐ | ☐ | connected, NOT exercised |
| Dropbox (managed) | ☐ | ☐ | connected, NOT exercised |
| Directory connectors enabled (Slack/Notion/GitHub/…) | ☑ | n/a | `Claude Docs` connected but **unmapped** → zero legs, ask (the correct default). Slack connected, not exercised. `trust-check` FAILs on both, by design |
| Sheets (local MCP) | — | — | not wired; `mcpServers` empty |
| IMAP/SMTP (local MCP, if wired) | — | — | not wired |
| Bash / Read (built-in) | ☑ | ☑ | `Bash` → `[]`, ask; `Read` → allow; `WebFetch` → `untrusted_content`, ask; `ToolSearch` → allow |
| **(2a) Agent/Task spawn (RB2)** | ☑ | — | Fired, via `always-gate: action class 'Agent' requires approval` — not ordinary `policy:review` |
| **(2b) Subagent-internal call (RB2)** | ☑ | — | **Fires.** The subagent's own `Read` reached the MAIN-session hook (spawn 06:34:50 → `Read` 06:35:22 → `SubagentHandback` 06:35:25) and was adjudicated. `SubagentHandback` is itself gated (unmapped → ask), so the return path is adjudicated too |

## Decision gate

- **RB1 provenance:** mechanism = **per-turn `UserPromptSubmit` writer**
  (`trust/bin/mark-turn` → `trust.mark_turn`, writes `{"kind":"owner","turn_id":N}`). Resolved.
  Verify live with step 3. ✅ mechanism chosen
- **RB2 subagent coverage:** v1 mitigation = **always-gate the `Agent`/`Task` spawn**
  (→ ask), so delegation is owner-approved per spawn rather than unattended
  (`always-gate.toml`; covered by `test_gate.py::TestSubagentContainment`). The spawn
  (2a) is the reliable containment point. The open sub-question is (2b): if
  main-session hooks do **not** fire on subagent-*internal* calls, then a single
  approved spawn can still let a subagent act ungated internally. Acceptable fallback
  for v1 (delegation is owner-approved and rare); if you need unattended delegation,
  add the deferred MCP-allowlist / per-connector enforcement that survives delegation
  before relying on it. ✅ mitigation chosen; **(2b) CONFIRMED LIVE 2026-09-18 on
  Claude Code 2.1.275: main-session hooks DO fire on subagent-internal calls.**
  Containment is therefore stronger than this gate assumed — a subagent cannot act
  ungated behind an approved spawn on this version. Keep the spawn gate regardless: this
  is one version's behaviour, not a guarantee
- **RB3 owner signal:** signal = `kind = "owner"` from the per-turn writer; maps to
  `OWNER` via the shipped `tiers.toml [kinds]` with no adopter config. Resolved.
  Verify with step 4. ✅ signal chosen

Do not proceed to rely on the gate for any **connector** row above that is unchecked.
The RB2 spawn-gating (2a) is enforced by tests; (2b) is the residual to confirm on
your Claude Code version.

> **Scope of the 2026-09-18 run.** Drive, Todoist and Dropbox are connected on that
> instance but were never exercised, so their live tool names remain unconfirmed against
> the catalog — they stay unchecked rather than being marked off by association. Re-run
> this whole procedure after upgrading Claude Code: (2b) in particular is an observation
> about 2.1.275, and the v1 mitigation (always-gate the spawn) is what the tests enforce.
