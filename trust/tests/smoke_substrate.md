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

| Source | Hook fired? | Legs/decision correct? (step 5) | Notes |
|---|---|---|---|
| Gmail (managed) | ☐ | ☐ | read → private_read + untrusted; no send tool |
| Google Calendar (managed) | ☐ | ☐ | read → private_read; create/update → send; delete → hard-gate |
| Google Drive (managed) | ☐ | ☐ | read → private_read + untrusted; create/copy → send |
| Todoist (managed) | ☐ | ☐ | read → private_read; add/update/comment/assign → send |
| Dropbox (managed) | ☐ | ☐ | read → +untrusted; create/move/share → send; delete + share-link hard-gate |
| Directory connectors enabled (Slack/Notion/GitHub/…) | ☐ | ☐ | names validated via `/mcp`; legs per the catalog block |
| Sheets (local MCP) | ☐ | ☐ | |
| IMAP/SMTP (local MCP, if wired) | ☐ | ☐ | |
| Bash / Read (built-in) | ☐ | ☐ | |
| **(2a) Agent/Task spawn (RB2)** | ☐ | — | Expected yes — the containment point. |
| **(2b) Subagent-internal call (RB2)** | ☐ | — | If NO: delegation acts ungated internally — see RB2 fallback. |

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
  before relying on it. ✅ mitigation chosen; (2b) to confirm live
- **RB3 owner signal:** signal = `kind = "owner"` from the per-turn writer; maps to
  `OWNER` via the shipped `tiers.toml [kinds]` with no adopter config. Resolved.
  Verify with step 4. ✅ signal chosen

Do not proceed to rely on the gate for any **connector** row above that is unchecked.
The RB2 spawn-gating (2a) is enforced by tests; (2b) is the residual to confirm on
your Claude Code version.
