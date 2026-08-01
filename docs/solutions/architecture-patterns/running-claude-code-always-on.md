---
title: Running Claude Code always-on — run model, liveness, and auth durability
date: 2026-06-23
last_updated: 2026-06-28
category: architecture-patterns
module: deploy / onboarding operability layer
problem_type: architecture_pattern
component: assistant
severity: high
related_components:
  - authentication
  - tooling
  - development_workflow
applies_when:
  - Standing up an always-on Claude Code instance reachable via Remote Control
  - Choosing a run model for a long-lived headless agent on a Linux server
  - Writing a liveness/health probe for a Remote Control session
  - Reasoning about whether managed-connector auth survives a restart
tags:
  - claude-code
  - remote-control
  - systemd
  - always-on
  - oauth
  - liveness
  - secrets
  - managed-connectors
---

# Running Claude Code always-on — run model, liveness, and auth durability

## Context

The buzai-exp template stands up a single-principal Claude Code "chief of staff"
that must stay reachable from the Claude desktop/mobile apps via Remote Control,
survive restarts, and use both claude.ai-managed connectors (Gmail/Calendar/Drive/
Todoist) and local MCP servers (Sheets/Xero). Building the onboarding layer
(`deploy/`, `scripts/`) surfaced a set of operational unknowns that are non-obvious,
cost real research to settle, and are easy to get subtly wrong. This captures the
resolved answers so the next adopter (or the next instance) doesn't re-derive them.
(§§1–5 are the original onboarding-layer findings; §6 and the inline corrections to
§2/§3 were added 2026-06-28 after live smoke-testing the service on a real box.)

## Guidance

### 1. Run a long-lived `remote-control` server — never `--spawn session`

`claude remote-control` has **no never-exit run mode**. The `--spawn session`
variant is single-session and **exits on completion**, silently dropping any input
queued against the dead session — the original PoC's worst footgun. Run the
plain long-lived server form under a process supervisor with `Restart=always`:

```ini
# deploy/claude-remote.service.template (excerpt)
ExecStart=%h/.local/bin/claude remote-control --name buzai-assistant
Restart=always
```

A `--user` systemd unit is the primary run model (manual CLI is the quick/non-Linux
fallback; Docker is deferred). Enable `loginctl enable-linger <user>` so the unit
runs without an active login session.

### 2. The reconnect (environment) URL rotates on every restart — recover it from journald

The relay **environment id rotates on every (re)start**, so after a restart you must
reconnect the apps to a *new* URL, and there is **no status command** that prints it.
Parse it out of the service logs instead (`scripts/env_url.py`). Treat the URL as
**owner-equivalent access**: print it only to a TTY, never pipe it to logs, chat, or
monitoring.

> **Update (2026-06-28, live-tested):** current Claude Code attaches primarily via
> **account-based discovery** — open `claude.ai/code` or the mobile app's Code tab while
> signed into the same account and the named session appears; there is no QR to scan for
> it. The `?environment=env_…` deep-link still exists and is still owner-equivalent (the
> helper refuses to print it outside a TTY without `--force`), but the real perimeter is
> now the **Claude account login**, not URL secrecy — see trap (g) in §6.

### 3. Liveness = an ESTAB :443 owned by the remote-control PID — NOT the banner, NOT a hostname

The journald **"Ready" banner is not a liveness signal** — it is often never emitted on a
long-lived session. The working signal is an **established `:443` relay socket owned by
the remote-control process's own PID** (`scripts/liveness.py`).

> **Correction (2026-06-28, live-tested):** an earlier version of this guidance scoped the
> socket match to a relay **hostname** (`claude.ai`) in the `ss` peer column. That never
> fires — `ss -tnp` emits **numeric peer IPs**, never hostnames — so the probe reported
> NOT-LIVE on a healthy registered service. Scope instead to the remote-control
> **process's PID** holding an `ESTAB :443` (match the peer *port* + the owning `pid=` in
> the process column). This is both correct and spoof-proof: no other process's :443 and
> no look-alike host can register LIVE.
>
> Also corrected: **an idle session is LIVE.** An always-on assistant is legitimately idle
> for long stretches, so a "recent last-good-turn" cannot be a *requirement* — the old
> logic falsely reported NOT-LIVE when idle. Turn freshness is now **informational only**
> (flagged as stale, never failed). Wedged-but-connected detection is the deferred
> self-healing track.

### 4. Managed-connector auth: one-time attended, then it persists (within an idle ceiling)

- **Managed connectors only load under the Claude.ai-subscription auth method**, not
  an API key. An API-key instance simply won't see Gmail/Calendar/Drive/Todoist.
- There is **no headless OAuth flow**. Establish auth **once, attended** (e.g. over an
  SSH-forwarded browser step), after which `~/.claude/.credentials.json` (mode 0600)
  persists it.
- Tokens have roughly an **~8h idle ceiling** (Claude Code issue #31095). A restart
  **within the token-validity window on a clock-synced host** survives with no
  re-auth of either connector class; beyond the idle ceiling, re-auth is needed.
- Therefore the honest MVP scope is **survives-restart**, not indefinite always-on.
  The self-healing track (watchdog, auto-recovery) is deferred.

### 5. Secrets live outside the workspace, fail-closed, and the preflight must see real inputs

- Keep secrets **outside the repo** (`~/.config/buzai/secrets/*.env`) so they can
  never be git-tracked, loaded via systemd `EnvironmentFile` with **no `-` prefix**
  (missing file → fail-closed start), at mode **0600**.
- Gate startup with an `ExecStartPre` preflight (`scripts/secrets_preflight.py`) that
  checks perms + untracked status, and is reused as the smoke test's SC0 gate.
- **A guard only protects you if it receives inputs that can actually fail it.** The
  preflight's git-tracked check originally fed only the `~/.config` secret paths —
  which live *outside* the repo and can never be tracked — so the check was a dead
  no-op. The risk it exists to catch is an in-repo `deploy/env/*.env` committed by
  mistake, so the check must inspect *those* candidate paths.

### 6. Headless remote-control operability cluster (live-tested 2026-06-28)

Standing up the service on a real managed-connector-only box surfaced a cluster of traps
that all present as **"service `active` / looks fine" while silently non-functional.** None
were catchable by unit tests; only a live smoke test found them.

- **(a) Inference-only tokens silently never register Remote Control.** A `setup-token` /
  `CLAUDE_CODE_OAUTH_TOKEN` runs interactive sessions and tools fine, but the remote
  server starts `active` and **never registers** (zero relay sockets, nothing at the
  attach surface). A full-scope interactive `claude auth login` is required. Don't set
  `CLAUDE_CODE_OAUTH_TOKEN` in the shell/unit either — if present it *overrides* the login.
- **(b) Two one-time interactive consent prompts block a headless start.** `claude
  remote-control` first-run asks "Enable Remote Control? (y/n)" and "Spawn mode [1/2]" —
  a headless service can't answer them, so it starts `active` but never registers. Prime
  them once in a TTY (they persist per-project). Pass `--spawn=same-dir` in the unit to
  pin spawn mode; the "Enable Remote Control?" consent has **no CLI flag** and must be
  primed by hand.
- **(c) `KillSignal=SIGINT`, or `systemctl stop` hangs ~90s.** `claude remote-control`
  exits on SIGINT/EOF and **ignores SIGTERM** (the systemd default), so stop waits the
  full `TimeoutStopSec` then SIGKILLs (→ `failed`). Set `KillSignal=SIGINT`, a short
  `TimeoutStopSec`, and `SuccessExitStatus=SIGINT`.
- **(f) "No local secrets" must be OK in the preflight.** A managed-connector-only box
  (all connectors hosted, OAuth in the credentials file) legitimately has zero local
  secret env-files. The preflight must guard the *safety* of whatever secrets exist
  (perms, not-git-tracked), **not their existence** — failing on "no secrets found"
  blocks the simplest, most common deployment.
- **(g) Attach is account-gated — the account login is the perimeter.** Anyone signed into
  the hosted account (web Code surface or mobile Code tab) can attach and reach every
  connector and all personal data. Protect that login with 2FA. The `?environment=env_…`
  deep-link is a secondary, owner-equivalent capability (rotates per restart; display-only).

> **Cross-cutting meta-lesson:** every bug in this cluster (and the trifecta self-brick
> in the related doc) presented as "the unit is active / configs are valid" while silently
> broken. Treat `systemctl is-active` as necessary but never sufficient. For always-on
> remote-controlled agents, **live smoke testing on a real instance is irreplaceable.**

## Why This Matters

Each of these is a silent-failure trap, not a loud one:

- `--spawn session` *looks* like it works — it accepts a connection and runs a turn —
  then drops later input with no error.
- A liveness probe keyed on the banner or a bare substring reports green on a dead or
  spoofed session, defeating the entire point of the SC1 reboot-survival check.
- Assuming headless OAuth or API-key managed connectors wastes a deploy cycle before
  you discover the auth method is wrong.
- A preflight that can never fire gives false confidence that SC0 ("no secrets
  tracked by git") is enforced when it is not.

Getting them right is what makes "always-on" an honest claim rather than a
demo that quietly degrades.

## When to Apply

- Any always-on / long-lived headless Claude Code deployment reachable by Remote Control.
- Writing or reviewing a liveness/health check for such a deployment.
- Designing secrets handling and a startup gate for an agent that holds credentials.
- Setting adopter expectations about restart vs. indefinite uptime.

## Examples

**Dead-no-op guard (before → after).** The git-tracked check received only
out-of-repo paths, so it never inspected what could actually leak:

```python
# before — only ~/.config secrets (outside the repo, never trackable) → check never fires
probs += tracked_problems(secret_files, _git_tracked)

# after — also inspect the in-repo deploy/env/*.env candidates, run git from repo root
repo_secrets = _repo_secret_candidates()          # deploy/env/*.env, minus *.env.example
probs += tracked_problems(secret_files + repo_secrets, _git_tracked)
```

**Liveness socket match (before → after).** Hostname matching never fires (`ss` gives
numeric IPs) → scope to the remote-control process's PID:

```python
# before — peer-host == "claude.ai" never matches: ss -tnp emits numeric IPs → always NOT-LIVE
peer_host = fields[4].rsplit(":", 1)[0].strip("[]")
if peer_host == relay_peer or peer_host.endswith("." + relay_peer): ...

# after — match the peer PORT + the owning PID (the remote-control process), spoof-proof
if fields[0] == "ESTAB" and fields[4].rsplit(":", 1)[-1] == "443" and f"pid={pid}," in line:
    return True
```

## Related

- `deploy/README.md`, `deploy/claude-remote.service.template` — the run model.
- `docs/SETUP.md`, `docs/SMOKE-TEST.md` — the clone→attached runbook and SC0–SC8 checks.
- `scripts/env_url.py`, `scripts/liveness.py`, `scripts/secrets_preflight.py` — the helpers.
- `trust/README.md` — the trust gate; note PreToolUse hooks do **not** fire on
  subagent/Task calls (Claude Code issue #34692), an operational caveat for gate coverage.
- `docs/plans/2026-06-23-002-feat-adopter-onboarding-plan.md` — the originating plan.
- [Enforcing the lethal trifecta without self-bricking](../design-patterns/enforcing-lethal-trifecta-without-self-bricking-2026-06-28.md) — same "active but silently broken" failure shape; the gate that runs in this always-on session.
- [Connector classification as security](../best-practices/connector-classification-as-security-2026-06-28.md) — the leg catalog the gate consumes.
- [Verify a collaboration tool's real access model](../best-practices/verify-collaboration-tool-access-model-2026-06-28.md) — verify-don't-assume, applied to external content tools.
- PRs #12 (managed-only secrets), #13 (operability rewrite: liveness PID-scope, SIGINT, consent priming, account-gated attach).
