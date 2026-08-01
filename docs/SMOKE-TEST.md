# Smoke test — is your instance stood up and working?

Your instance is "stood up and working" when all eight criteria pass. Run after
`docs/SETUP.md`. SC0 is a preflight **gate** — don't start the service on an unsafe
secrets layout. Record results with `make smoke` (it runs the mechanizable checks and prints the
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
| SC6 | Hubs persist | Write to a hub, restart, confirm it persisted |
| SC7 | Substrate smoke-test + audit perms | `trust/tests/smoke_substrate.md` run & recorded (incl. subagent coverage); `audit/` is 0700 |
| SC8 | Auth survives a routine restart | Restart within the token-validity window on a clock-synced host → no re-auth of either connector class |

After SC3/SC4, also confirm **no secret values leaked to journald**:

```bash
journalctl --user -u claude-remote --since "<test start>" | grep -iE '(password|secret|token|api_key)=' || echo "clean"
```

Notes: a no-op round-trip can't be issued headlessly, so SC1's "recent last-good-turn"
is the round-trip stand-in. The reconnect window (SC2) and the ~8h idle-token ceiling
(SC8) are documented limits of the current system, not bugs — see `docs/SETUP.md` Scope.
