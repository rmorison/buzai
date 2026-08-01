# Troubleshooting

When something looks off. Run all of this as the instance's user on the server, from
your clone. `make doctor` is your first stop — it runs the common checks together:

```bash
make doctor      # prereqs + venv + secrets-preflight + liveness
make liveness    # just the LIVE / NOT-LIVE probe
```

---

## "I started the service but nothing shows up at claude.ai/code" (the #1 trap)

Almost always one of two things — the service is `active` but **silently never registered**:

1. **You skipped the one-time interactive consent.** `claude remote-control` has first-run
   prompts ("Enable Remote Control? y", "Spawn mode 1") that a headless service can't
   answer, so it starts but never registers. Fix: run it once by hand and answer them, then
   restart the service (this is [`SETUP.md`](SETUP.md) §6):

   ```bash
   make service-stop
   make prime-consent      # answer y, then 1; confirm it appears at claude.ai/code; exit Ctrl-D Ctrl-D
   make service-start
   ```

2. **Inference-only auth.** A `claude setup-token` / `CLAUDE_CODE_OAUTH_TOKEN` runs sessions
   but **cannot register Remote Control**. You need a full-scope interactive login: `make auth`
   (and make sure `CLAUDE_CODE_OAUTH_TOKEN` is **not** set in your shell or the unit — it
   overrides the login). Then `make service-restart`.

Confirm it registered: `make liveness` reports `LIVE`, and the service holds a relay
socket — `ss -tnp | grep "$(systemctl --user show -p MainPID --value claude-remote)"` shows
an `ESTAB …:443`.

## Reconnecting after a restart

Attachment is **account-based**: just reopen **claude.ai/code** or the mobile **Code tab**
(signed into the same account) and the session reappears — there's no URL to re-scan. The
per-session deep-link (`make env-url`) rotates on every restart, so a stale one stops
working; the account path always finds the current session.

## "It's asking me to log in again" (re-auth)

A normal restart or reboot needs **no** re-login — tokens persist in
`~/.claude/.credentials.json` and re-mint on a bounce. You only re-auth when:

- **A very long idle uptime lapsed the token** — recovery is a bounce: `make service-restart`.
- **The host clock drifted** — clock skew breaks token validation. Keep the host NTP-synced.
- **A managed connector's OAuth expired or was never completed** — re-do it in the Claude.ai
  web app (Settings → Connectors). Managed-connector OAuth **cannot** be re-established
  headlessly.

## "`systemctl stop` hangs for ~90 seconds"

`claude remote-control` exits on **SIGINT / Ctrl-D**, not SIGTERM. The shipped unit sets
`KillSignal=SIGINT` so stop/restart is prompt — if yours hangs, your unit predates that
fix; re-run `make service-install` to re-copy the current template and reload the unit.

## Self-diagnosis checklist

`make doctor` covers items 1–2 and the secrets check; when health is still ambiguous,
walk the rest (carried from real operation):

1. **Unit active?** `systemctl --user is-active claude-remote`
2. **Relay socket established?** `ss -tnp | grep <main-pid>` → an `ESTAB …:443` owned by the
   `claude` process. (No socket = not registered → see the #1 trap above.)
3. **Token expiries in the future?** (the credentials file) — if lapsed, bounce.
4. **Model resolves** to an available model?
5. **Connectors connected — and classified?** `claude mcp list` shows connections
   (managed ones carry a `claude.ai` prefix); `make trust-check` then diffs your
   trust-gate rules against those live servers — an unclassified connected server or a
   dead rule pattern is exactly the "gate asks for everything" / "legs never apply"
   symptom.
6. **Restart loop?** `journalctl --user -u claude-remote` — repeated starts mean a crash
   loop (the unit's start-limit will park it in `failed` rather than churn silently).
7. **Host basics:** load, memory, disk, and the **clock** (skew breaks tokens).

## Known red herrings — don't chase these

- A telemetry host (e.g. `statsig.anthropic.com`) failing to resolve — by design, harmless.
- Client-side scroll/render lag on a large transcript — cosmetic, client-side.
- A phantom "running" badge with no live process — an orphan from a dead session; the
  liveness probe is the source of truth, not the badge.

---

If you're stuck after this, send the owner: the output of `make doctor`, the last
~30 lines of `journalctl --user -u claude-remote` (**redact any URL/token** — the
environment URL is owner-equivalent), and what you were doing.
