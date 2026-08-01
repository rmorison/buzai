# Quickstart

Zero to an attached, trust-gated, always-on assistant in about 30 minutes.
Steps only — every pause links its reference section in [`SETUP.md`](SETUP.md).

**You need:** a Linux server you control (internet-reachable), a Claude.ai
subscription (Pro/Max/Team/Enterprise), and a browser on any machine.

## 1. Bootstrap the host (as any sudo account)

```bash
curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | sudo bash
```

Creates the dedicated `buzai` user, enables linger, wires the systemd user bus,
verifies both. Idempotent. ([HOST-BOOTSTRAP.md](HOST-BOOTSTRAP.md))

## 2. Switch in and run setup (as the `buzai` user)

```bash
sudo -u buzai -i
curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | bash
```

That fetches the repo to `~/buzai` and runs **`make setup`**, which probes each
step, skips what's done, and **pauses only at the three manual steps** — do each,
then re-run `make setup` (it resumes where you left off):

1. **Sign in** — `make setup` stops and tells you to run `make auth`: pick
   **"1. Claude account with subscription"**, open the printed URL in your browser,
   paste the code back. ([SETUP.md §3](SETUP.md))
2. **Connectors** *(optional — skip freely)* — enable what you want at
   [claude.ai/settings/connectors](https://claude.ai/settings/connectors), then
   `make trust-check` classifies them against the gate. ([SETUP.md §4](SETUP.md))
3. **Remote Control consent** — `make prime-consent`: answer **y**, spawn mode
   **1**, confirm the session shows at [claude.ai/code](https://claude.ai/code),
   exit with **Ctrl-D Ctrl-D**. ([SETUP.md §6](SETUP.md))

Setup finishes by starting the service and running the live check — it ends with
**LIVE** (`make liveness`), which does not depend on anyone being attached.

## 3. First use: attach

Open **[claude.ai/code](https://claude.ai/code)** or the **Claude mobile app → Code
tab**, signed into the **same** account — your `buzai-assistant` session appears
automatically. ([SETUP.md §7](SETUP.md))

## 4. Verify it's really working

```bash
make smoke      # preflight for the 8-point check
```

Ask the assistant one small thing (e.g. *"list my calendars"*), then `make audit` —
the action shows as `tier=OWNER`: that's the trust gate on every call. Full
verification: [SMOKE-TEST.md](SMOKE-TEST.md). See what the gate protects and try to
trip it on purpose: [SECURITY-MODEL.md](SECURITY-MODEL.md) + [TRY-IT.md](TRY-IT.md).

---

Anything unclear or broken here is a bug — please file it.
