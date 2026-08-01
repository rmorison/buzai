---
date: 2026-06-23
topic: adopter-onboarding
---

# Adopter Onboarding & Operability — Requirements

## Summary

Define what an adopter needs to stand up their own always-on Claude Code "chief of staff" from the buzai-exp template and use it — enough to smoke-test the MVP end to end. The adopter clones the repo onto an internet-connected Linux server with Claude Code installed, follows a setup runbook (connectors, hubs, trust-gate wiring), runs it as a `--user` systemd service, attaches Remote Control from the Claude desktop/mobile apps, and verifies the result against a defined smoke test. The near-term target is the author's own single instance — one box stood up and smoke-tested end to end; broad-adopter ergonomics are secondary. The MVP validates that the assistant starts and survives a restart, not that it runs unattended indefinitely (durable always-on is the deferred self-healing track). Draws on the operational doctrine the author's earlier private PoC already paid for.

## Problem Frame

The PoC works, but it is bespoke to one box: the run scaffolding, secrets wiring, and hard-won operational knowledge live in that deployment and in a postmortem, not in a form another person can follow. Worse, the PoC's own systemd unit carries a known footgun — a single-session spawn that exits on completion plus auto-restart, which silently swallows prompts during the reconnect window. For the template to be adoptable — and for the trust gate and (later) the compound step to be exercisable at all — there has to be a clean getting-started path and a definition of "stood up and working." This brief is that operability layer; it does not re-decide the gate or the assistant's features.

## Key Decisions

- **Run as a `--user` systemd service (primary).** Restart-on-crash, journald log capture, start-on-boot, and `loginctl enable-linger` so it runs without an active login. This is the clean form of the nohup+pipes+restart an adopter would otherwise hand-roll on a remote box. Manual foreground CLI stays as the simple path for quick/local/non-Linux runs. Docker is deferred (it would need credential volume-mounts to match the auth-durability the `--user` home gives for free).
- **Persistent session, not exit-on-complete.** The MVP deliberately diverges from the PoC's `--spawn session` + `Restart=always` pattern, which the 2026-06-16 postmortem pins as the cause of silent prompt loss. The requirement is a session that does not exit on task completion, or input gated until the session is ready — never a window that accepts prompts with no live session to run them.
- **Liveness is an established relay socket, not the log banner.** Health is judged by an `ESTAB :443` connection from the daemon PID to the relay (plus a recent successful turn), because the "Ready" banner is frequently never emitted on a live session and caused hours of false "stuck Connecting" debugging.
- **Reconnect-after-restart is a first-class step.** The relay environment id rotates on every (re)start; the adopter must reconnect to the new environment URL/QR, and an old window silently swallows input. The runbook and the smoke test treat this as expected behavior, not a bug.
- **Secrets via supervisor env-files, never in VCS or the unit.** Local-MCP credentials are injected through systemd `EnvironmentFile` drop-ins pointing at a gitignored secrets dir — the pattern the PoC uses for email/Xero.
- **Import the MVP-critical operability requirements from the PoC; defer the rest.** The PoC's `planning/requirements.md` holds R1–R12 from real operation. This brief adopts the ones an adopter hits on day one (correct liveness, no-silent-loss, reconnect UX, personal/generalizable separation) and defers the self-healing feature set to a sibling operability track (see Scope Boundaries).
- **The MVP claim is survives-restart, not indefinite always-on.** Passing the smoke test proves the assistant stands up and recovers across a restart; it does not prove unattended longevity. The relay token has a finite lifetime, and the failure modes the PoC postmortem hit worst (token lapse, model unavailability) are addressed by the deferred self-healing track, not here.

## Actors

- A1. Adopter / self-hoster — clones the template onto their own server and runs the setup. The single principal of their instance. Technical enough to use a terminal and edit config files; not assumed to know this codebase.
- A2. The running assistant — the always-on `claude remote-control` session the adopter operates from the Claude apps.

## Requirements

**Run & supervise**
- R1. The template ships a `--user` systemd unit (and a documented `enable` + `enable-linger` step) that starts the assistant on boot, restarts it on crash, and routes stdout/stderr to journald. The runbook notes that `enable-linger` typically needs elevated privileges and gives a fallback when the adopter lacks them.
- R2. The unit runs a persistent remote-control session that does not exit on task completion; if only an exit-on-complete mode is available, client input is gated/buffered until the session is ready so no prompt is accepted into a dead window (PoC R3, R4).
- R3. A manual foreground CLI invocation is documented as the simple path for quick, local, or non-Linux runs.

**Setup & connectors**
- R4. A setup runbook walks the adopter from clone to first connection: prerequisites (Linux server, Claude Code installed, a Claude.ai subscription login), configuring connectors, seeding hubs, wiring the trust gate, and starting the service.
- R5. The runbook covers both connector classes: claude.ai-managed connectors (Gmail/Calendar/Drive/Todoist), which require the Claude.ai-subscription auth method and an interactive OAuth step, and local MCP servers (Sheets/Xero/IMAP), whose secrets are supplied via the env-file drop-ins.
- R6. The runbook includes wiring the trust gate per `trust/README.md` — registering its PreToolUse hook (merge, don't overwrite `.claude/settings.json`), supplying per-turn provenance, and configuring the owner channel — and running the substrate smoke-test.
- R7. Hubs are seeded from shipped empty/example scaffolds; the adopter's populated hubs are personal state, gitignored, never shipped.

**Remote control & reconnect**
- R8. The runbook explains attaching Remote Control from the desktop and mobile Claude apps, reconnecting after any restart via the new environment URL/QR (the environment id rotates per restart; the old window must not be reused), and how to retrieve the current environment URL/QR after a restart (from the service logs or a status command).
- R9. The documented liveness check is the established relay socket (and last-good-turn), not the journal banner; known red herrings (telemetry hosts that don't resolve, client-side render lag, orphan "running" badges) are called out so adopters don't chase them. The runbook includes a manual self-diagnosis checklist — unit active, relay socket established, token expiries in the future, model resolves, MCP servers connected, restart-loop check, host clock/disk/memory — carried from the PoC's operations doctrine and distinct from the deferred `assistant doctor` automation.

**Auth durability**
- R10. A normal restart or reboot requires no re-authentication: local-MCP env-file secrets reload via the unit, and the Claude OAuth + relay tokens in the credentials file persist and are re-minted on a bounce. The runbook notes that very long uptimes can lapse the relay token (recovery: bounce) and that host clock skew breaks token validation.

**Packaging & separation**
- R11. Generalizable machinery (run scaffolding, skills, conventions, the trust gate) is separable from personal state (hubs, secrets, deployment memory); the template ships only machinery plus empty scaffolds/examples (PoC R12, `docs/decisions/knowledge-management.md`).

**Security & exposure**
- R12. The setup documents the trust boundary of the always-on session: it is bound to the owner's Claude.ai account (the sole principal permitted to attach), and the reconnect environment URL/QR is owner-equivalent access — treated as a secret, never logged, shared, or committed. An explicit threat note states that anyone who can attach reaches every connector and the personal hubs.
- R13. Secrets are protected at rest: env-file drop-ins and the Claude credentials file are mode 0600, owned by the service user; the template ships no example secrets file containing real key names; connector startup/restart/error output must not log secret values to journald; and the smoke test asserts the secrets directory is gitignored and untracked before first start.

## Key Flows

- F1. First run (getting started)
  - **Steps:** clone onto the server → install/confirm Claude Code + Claude.ai login → configure managed connectors (OAuth) and local-MCP env-files → seed hubs → wire + smoke-test the trust gate → `enable --now` the systemd unit (+ linger) → attach Remote Control from the app → use from desktop/mobile.
  - **Covered by:** R1, R4, R5, R6, R7, R8.
- F2. Restart / reconnect
  - **Trigger:** the service restarts (crash, reboot, manual bounce).
  - **Steps:** systemd auto-starts the session → tokens persist/re-mint → the adopter reconnects to the new environment id via URL/QR; no prompt entered during the reconnect window is silently lost.
  - **Covered by:** R2, R8, R10.

## Success Criteria (the MVP smoke test)

The MVP is "stood up and working" when an adopter can demonstrate all of:

- SC1. The service starts via the systemd unit, auto-starts on boot, and survives a reboot — liveness confirmed by an established relay socket *and* a successful no-op round-trip (last-good-turn), since an open socket alone does not prove the session can receive input; not the log banner; logs visible in journald.
- SC2. Remote Control attaches from both the desktop and mobile Claude apps, and reconnects to the new environment after a restart.
- SC3. A managed-connector action (e.g., read calendar) works end-to-end through the trust gate.
- SC4. A local-MCP action (e.g., Sheets or Xero) works through the gate.
- SC5. The gate is wired and enforcing: a legitimate owner-channel action is permitted (proving the gate is present, not silently absent), a review / new-recipient action prompts for approval, an all-three-legs attempt is denied (per the trust model's acceptance examples), and a second distinct action in the same session still gates (enforcement is ongoing, not one-shot).
- SC6. Hubs are readable and writable and persist across a restart.
- SC7. The substrate smoke-test (`trust/tests/smoke_substrate.md`), including the subagent-coverage check, is run and its results recorded.
- SC8. Connector auth survives a routine restart: a restart performed within the relay-token validity window, on a clock-synced host, requires no re-authentication of either connector class. (Long-uptime token lapse, clock skew, and any attended-OAuth requirement are out of this criterion — see Dependencies / Assumptions.)

## Scope Boundaries

### Deferred to Follow-Up Work
- The self-healing / observability track — token-expiry watchdog, orphaned-task reaper, model-availability fallback, an `assistant doctor` self-diagnosis command, and health introspection (PoC R2, R5, R6, R7, R8, R9). Deferred because a working stand-up path is a prerequisite to even exercising these failure modes; MVP onboarding documents the manual checks (R9) and states the survives-restart-not-indefinite-uptime limit, and automating them is the next operability brief.
- Docker / container packaging.
- A hosted or multi-tenant offering.
- A GUI installer or setup wizard — the runbook is text.
- Auto-provisioning connector OAuth — the adopter performs the interactive auth step manually.

## Dependencies / Assumptions

- Depends on Claude Code Remote Control and the Claude desktop/mobile apps; managed connectors require the Claude.ai-subscription auth method (not an API key) and an interactive OAuth that may not be establishable in a purely headless session — to confirm during the smoke test.
- Depends on the trust gate (PR #1, `docs/plans/2026-06-23-001-feat-trust-capability-model-plan.md`); onboarding includes its install + the RB1–RB3 substrate verification.
- The environment-id-rotation reconnect behavior and the relay-socket liveness signal are taken from the PoC's verified operation; re-confirm on the adopter's stack during the smoke test.
- Assumes the host clock is NTP-synced (clock skew breaks token validation) and that the relay token's finite lifetime bounds unattended uptime before a bounce is needed — the boundary SC8 is scoped against.

## Outstanding Questions

**Resolve before planning (go/no-go gates — confirm with a short spike before committing the design)**
- Does a persistent (non-exit-on-complete) remote-control mode exist? If not, the MVP must implement input-gating/buffering to satisfy R2 — the design branches on this, so confirm it first.
- Can managed-connector OAuth be (re)established on a headless server, or does it require a one-time attended step? A dead end here blocks the managed-connector half of the smoke test (SC3); resolve before relying on it.

**Deferred to implementation / smoke test**
- Whether a normal service restart preserves the relay token cleanly enough that SC8 holds without a manual `/login`, and the actual relay-token lifetime (the unattended-runtime ceiling).
- Reconfirming env-id rotation and relay-socket liveness on the adopter's Claude Code version/host (the PoC observed them on one stack).

## Sources / Research

- The author's earlier private PoC (pre-buzai): `planning/operations.md` (run model, liveness, auth/token, reconnect mechanics, self-diagnosis runbook), `planning/postmortems/2026-06-16-remote-control-stuck-connecting.md` (silent prompt loss, stale model, token lapse, the right liveness signal), `planning/requirements.md` (R1–R12 operability requirements from real operation), and the live `--user` `claude-remote.service` unit + `EnvironmentFile` drop-ins.
- `trust/README.md` and `trust/tests/smoke_substrate.md` — the gate install and substrate-verification steps the runbook incorporates.
- `docs/decisions/knowledge-management.md` — the personal/generalizable separation the packaging requirement preserves.
