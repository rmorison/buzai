# buzai — a safe, always-on personal AI assistant

buzai is a self-hosted, always-on "chief of staff" built on Claude Code. It connects to
your email, calendar, files, and tasks, runs on your own Linux server, and you drive it
from the Claude desktop and mobile apps. What makes it different is that **every action it
takes passes through a trust gate** designed around the way AI assistants actually get
attacked — so a poisoned email or calendar invite can't quietly turn your assistant into
a data-exfiltration tool.

> **Open source, MIT-licensed** — see [`LICENSE`](LICENSE). It is **single-principal**
> (one person per instance) and the current claim is **survives-restart**, not
> unattended-forever (see *Scope* below).

## Is this for you?

You'll want to be comfortable with a terminal and basic Linux server administration. To
stand up an instance you need:

- **A Linux server you control**, reachable from the internet (a small VPS is fine).
- **A Claude.ai subscription** (Pro / Max / Team / Enterprise) — the managed connectors
  and Remote Control require subscription auth, not an API key.
- A browser on any machine for a one-time sign-in.
- About **30 minutes** for first setup.

## Get started

**→ [`docs/QUICKSTART.md`](docs/QUICKSTART.md)** — one page. Two `curl | bash`
commands (host bootstrap, then setup as the service user); `make setup` automates
everything else and pauses only at the three inherently manual steps (subscription
sign-in, connector OAuth, the one-time Remote Control consent). Prefer to read every
step first? [`docs/SETUP.md`](docs/SETUP.md) is the full reference, and the whole
flow is plain `make` verbs you can run yourself.

## Then understand and try it

- **[`docs/SECURITY-MODEL.md`](docs/SECURITY-MODEL.md)** — how the trust gate protects
  you, in plain terms: the "lethal trifecta," trust tiers, the Rule of Two, and the audit
  log. Read this to understand *why* the assistant is safe to leave running.
- **[`docs/TRY-IT.md`](docs/TRY-IT.md)** — guided experiments that make each safety
  feature fire so you can see it work: approvals, a blocked send, the loud "are you sure?"
  on a risky combination, and the audit trail.
- **[`docs/TUNING.md`](docs/TUNING.md)** — how to relax or tighten the gate as you build
  confidence: which actions auto-run, which always ask, and how to classify your own
  connectors.
- **[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)** — reconnecting, re-auth,
  restarts, and a self-diagnosis checklist when something looks off.
- **[`docs/SMOKE-TEST.md`](docs/SMOKE-TEST.md)** — the 8-point check that an instance is
  "stood up and working."
- **[`CONCEPTS.md`](CONCEPTS.md)** — a short glossary of the terms used throughout.

## What's in the box

- **`trust/`** — the trust & capability gate (a Claude Code hook) and its configuration.
  [`trust/README.md`](trust/README.md) is the deeper reference.
- **`deploy/`** — the systemd service that keeps the assistant running, and secrets
  scaffolding.
- **`scripts/`** — operability helpers (liveness, reconnect URL, a secrets preflight).
- **`hubs/`** — scaffolds for the assistant's personal knowledge base (your real hubs stay
  on your machine, never in version control).

## Scope (what this is and isn't, yet)

- **Is:** an always-on, single-principal assistant that starts on boot, survives a
  restart, gates every tool call through the trust model, and keeps a redacted, tamper-
  evident audit log.
- **Isn't (yet):** a multi-tenant or hosted service; an unattended-for-weeks system
  (a very long-idle session's auth token can lapse — recovery is a one-line restart); a
  self-healing system (watchdogs, auto-recovery) — that's a later track.

## Project history

buzai is developed in the open — the engineering trail ships with the repo. The
original seed idea is [`docs/ai-assistant-idea.org`](docs/ai-assistant-idea.org); the
ideation run that ranked the trust model as the top direction is
[`docs/ideation/`](docs/ideation/), and the requirements, plans, and hard-won
operational learnings behind each feature live in [`docs/brainstorms/`](docs/brainstorms/),
[`docs/plans/`](docs/plans/), and [`docs/solutions/`](docs/solutions/). If you're
researching trust gating for agentic assistants, the solutions directory is the
distilled part — real footguns found by running this system, not theory.

## Feedback & contributing

Please file what you find — confusing docs, friction in setup, surprising gate behavior,
or "I wish it did X" — as GitHub issues. Setup traps and rough edges are exactly the
reports that improve this fastest. To contribute code or docs, see
[`CONTRIBUTING.md`](CONTRIBUTING.md); to report a security issue privately, see
[`SECURITY.md`](SECURITY.md).
