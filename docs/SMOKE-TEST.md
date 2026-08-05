# Smoke test — is your instance stood up and working?

Your instance is "stood up and working" when all nine criteria pass. Run after
`docs/SETUP.md`. SC0 is a preflight **gate** — don't start the service on an unsafe
secrets/personal-state layout. Record results with `make smoke` (it runs the mechanizable checks and prints the
checklist); the live items are attested by you. (The `make` verbs run under the pinned
`.venv` from SETUP step 5.)

| # | Criterion | How to check |
|---|---|---|
| SC0 | Secrets/creds 0600 + untracked (gate) | `make smoke` exits 0 (its SC0 preflight); blocks the rest on failure |
| SC1 | Service up + survives reboot | `systemctl --user is-active claude-remote`; reboot; `make liveness` → LIVE (relay socket + recent last-good-turn, not the banner) |
| SC2 | Remote Control attaches (desktop + mobile) + reconnects after restart | Attach from both apps; restart the service; reconnect via the apps (or `make env-url` for the deep-link); a prompt typed pre-restart is known-lost (documented) |
| SC3 | A managed-connector action works through the gate | e.g. "read my calendar" — completes, and the gate logged the decision |
| SC4 | A local-MCP action works through the gate | e.g. a Sheets/Xero read — completes through the gate |
| SC5 | Gate enforces | A legitimate owner action is permitted; a review/new-recipient action prompts; an all-three-legs attempt is denied; a second distinct action still gates |
| SC6 | Hubs persist **and reach the private remote** | Record a fact (below), restart, confirm it persisted; `make hub-remote-check` → verified PRIVATE; `make hub-push` → nothing unpushed; then clone the remote elsewhere and find the fact in it |
| SC7 | Substrate smoke-test + audit perms | `trust/tests/smoke_substrate.md` run & recorded (incl. subagent coverage); `audit/` is 0700 |
| SC8 | Auth survives a routine restart | Restart within the token-validity window on a clock-synced host → no re-auth of either connector class |
| SC9 | Review loop works from the apps | `make hub-review` lists the change in plain language (no diff); approve it from the Claude app and it stops appearing; reject a second one with a reason → the content is gone from the hub file and both the original and the correction are in `git -C ~/hubs log` |

After SC3/SC4, also confirm **no secret values leaked to journald**:

```bash
journalctl --user -u claude-remote --since "<test start>" | grep -iE '(password|secret|token|api_key)=' || echo "clean"
```

**SC6 — persistence is not the whole test; off-box is.** A hub write that never left the
box is one dead disk from gone, and a push can fail silently (an expired deploy key is the
*expected* failure, not an edge case). So assert both halves:

```bash
# 1. record a fact through the real write path (or just ask the assistant to)
.venv/bin/python scripts/hub_commit.py --file smoke.md \
  --append "- smoke test $(date -Is)" \
  --summary "Record a smoke-test fact" --reason "SC6" --source owner-directed

make service-restart && grep -c "smoke test" ~/hubs/smoke.md   # persisted across a restart
make hub-remote-check                                          # verified PRIVATE, not just reachable
make hub-push                                                  # → "nothing to push"

# 2. the off-box half: it is really in the remote, not only in the local repo
git clone <your-private-hub-remote> /tmp/hub-restore-check
grep -r "smoke test" /tmp/hub-restore-check && rm -rf /tmp/hub-restore-check
```

That clone doubles as the SETUP §9(f) restore rehearsal. Add
`git -C /tmp/hub-restore-check fetch origin "refs/notes/*:refs/notes/*"` to confirm your
SC9 verdicts came back too — `git clone` does not fetch the notes ref.

Notes: a no-op round-trip can't be issued headlessly, so SC1's "recent last-good-turn"
is the round-trip stand-in. The reconnect window (SC2) and the ~8h idle-token ceiling
(SC8) are documented limits of the current system, not bugs — see `docs/SETUP.md` Scope.
`make smoke` renders the SC0–SC8 checklist only; SC9 is listed by `make smoke` too; it is attested here alongside the other
live items.
