# buzai — Setup & getting started

Stand up your own always-on Claude Code "chief of staff" and use it from the
Claude desktop/mobile apps. One instance = one person = one dedicated Linux user.
The honest claim is that the assistant **starts and survives a restart** — not that
it runs unattended indefinitely (see Scope).

> **The fast path is [`QUICKSTART.md`](QUICKSTART.md)** — one page, driven by
> `make setup`, which probes each step, skips what's done, and pauses only at the
> three inherently manual ones (subscription login, connector OAuth, the Remote
> Control consent prime). Re-running `make setup` resumes where you left off.
> **This document is the full reference** those pauses point into.

**Flow:** clone → install Claude Code → authenticate (Claude.ai) → configure
connectors + hubs → wire the trust gate → start the systemd service → attach Remote
Control from the apps → verify with the smoke test. `make setup` drives all of it;
every step is also a single `make` verb you can run yourself — **`make help`** is
the index.

Attaching from the apps is deliberately *outside* setup: it's first use, and the
liveness check is service-side (an idle, never-attached instance is legitimately
LIVE).

> **Step 0 (recommended):** host one instance under its own dedicated Linux user —
> `curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | sudo bash`
> (see [`docs/HOST-BOOTSTRAP.md`](HOST-BOOTSTRAP.md)), then do everything below as that user.

## Prerequisites

- A Linux server you control, reachable from the internet.
- A **Claude.ai subscription** — managed connectors require subscription auth, not
  API-key auth. (Installing Claude Code and logging in are steps 2–3 below.)
- A browser on any machine (e.g. your workstation) for the one-time login — the
  server itself needs no GUI.
- Python 3.11+ (for the trust gate and the helper scripts; no other deps).

## Run setup as the `buzai` user — not your personal account

**Every step below runs as the dedicated `buzai` user.** That isolation *is* the trust
gate's blast radius — running setup as yourself defeats the point (the installer's user
phase refuses to run inside a sudo-capable account for exactly this reason). If you
provisioned the account via [`HOST-BOOTSTRAP.md`](HOST-BOOTSTRAP.md), it copied your
`authorized_keys` over, so the normal way in — now and for every day-2 session — is a
plain ssh login from your workstation:

```bash
ssh buzai@<host>
```

No key on the account (password-auth admin, `BUZAI_COPY_SSH_KEYS=0`, or a restrictive
`sshd_config`)? Switch in from your sudo account on the server instead:

```bash
sudo -u buzai -i
```

Any path that lands you in a `buzai` **login session** (with `systemctl --user`
working) is fine — ssh gets that wiring natively from `pam_systemd`; the `sudo -u`
fallback relies on the `XDG_RUNTIME_DIR` line bootstrap wrote into the account's
login init file (the first existing of `.bash_profile`/`.bash_login`/`.profile`).
Everything from step 1 on happens in that session.

## 1. Clone

The repo is public:

```bash
git clone https://github.com/rmorison/buzai.git ~/buzai && cd ~/buzai
```

(The installer's user phase — `curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | bash` —
does this for you, falling back to a tarball when git isn't installed, then runs
`make setup`.)

`~/buzai` is the assistant's workspace. Personal state (your hubs, your secrets)
stays gitignored; the repo ships only machinery + empty scaffolds.

## 2. Install Claude Code

Native installer — no Node.js, per-user, no root needed:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

It installs the binary to `~/.local/bin/claude` and adds that to your PATH in your
shell init (`~/.profile`). That only applies to **new login shells**, so load it into
the current one:

```bash
source ~/.profile          # applies PATH now; or re-login: exit && sudo -u buzai -i
```

Then verify:

```bash
which claude       # → /home/buzai/.local/bin/claude
claude --version
claude doctor      # deeper health check
```

(npm alternative: `npm install -g @anthropic-ai/claude-code` — needs Node 18+, and
never with `sudo`. Prefer the native installer on a headless box.) Auto-updates run in
the background; force one with `claude update`. Docs:
<https://code.claude.com/docs/en/setup>.

## 3. Authenticate (Claude.ai subscription)

Managed connectors and Remote Control both require Claude.ai **subscription** auth, so
log in interactively once. Just start Claude Code — on a fresh account it walks you
straight through first-run setup and login:

```bash
make auth          # drops you into `claude` for the one-time interactive login
```

1. Pick a theme (first run only).
2. At **"Select login method"**, choose **1. Claude account with subscription**
   (Pro / Max / Team / Enterprise) — *not* the Console/API option, which won't load
   managed connectors.
3. No browser on the server, so Claude prints **"Browser didn't open? Use the url
   below to sign in"** with an OAuth URL. Open that URL in a browser on your
   workstation and **Authorize** (the consent screen lists "Use and manage your
   connectors" — that's the connector capability).
4. The browser shows an **authorization code**; paste it at **"Paste code here if
   prompted >"**. You'll see **"Login successful. Press Enter to continue."**
5. Confirm anytime with `/status` (shows the active auth method).

> **The pre-login warning is normal.** Before you log in, Claude Code (and the daemon
> view) shows a `Remote Control ⚠` block — "Mode: ephemeral", "Not signed in to
> claude.ai", "subscription auth not active", "missing the user:profile scope",
> "Organization not resolved". That's expected on a fresh account and clears once
> login completes; it's not a misconfiguration.

**First time into the workspace.** The first `claude` run inside `~/buzai` hits a few
one-time prompts (exact wording is Claude-Code-version-specific and changes over time):

- **"Is this a project you created or one you trust?"** — answer **Yes, I trust this
  folder**, but *only because it's your own clone*. This is Claude Code's
  folder-trust gate (it governs reading/editing/executing files here); don't reflexively
  trust a directory you didn't create.
- An experimental offer like **"Try the new fullscreen renderer?"** — **decline it
  during setup** (`Not now`). The fullscreen renderer can break terminal copy/paste
  (e.g. Ctrl+Shift+C in GNOME Terminal), and setup is copy-heavy (OAuth URLs, codes,
  command output). Turn it on later, past the copy-heavy steps, if you like it.
- A **"Welcome back … · Claude Max · <org>"** banner — informational, and a handy
  second confirmation that subscription auth is live (alongside `/status`).

To re-authenticate later, use `/login` inside a session or `claude auth login` from
the shell. Credentials persist at `~/.claude/.credentials.json` (mode 0600) and
auto-refresh, so a restart *within* the token window needs no re-auth. This is the
one-time attended step — there is no fully headless OAuth (the OAuth callback is
hosted at `platform.claude.com`, which is why no SSH-forwarded callback is needed). An
idle session can still let the access token lapse (~8h); recovery is a bounce
(`systemctl --user restart claude-remote`) or re-running `/login`. Keep the host clock
NTP-synced (skew breaks token validation).

> **Always-on note — use the interactive login, not `setup-token`.** `claude
> setup-token` / `CLAUDE_CODE_OAUTH_TOKEN` mints a long-lived **inference-only** token.
> It runs interactive sessions and tools fine, but it is **confirmed to break Remote
> Control**: the always-on server starts `active` yet **silently never registers** (0
> relay sockets, nothing at claude.ai/code) — and it's also unverified for managed
> connectors. The full-scope **interactive subscription login above is required** for a
> connector-bearing, remote-controlled instance. (Don't set `CLAUDE_CODE_OAUTH_TOKEN`
> in the shell/unit either — if present it *overrides* `claude auth login`.)

## 4. Connectors

With Claude Code authenticated (step 3), wire up the connectors this instance needs.
There are two kinds, set up very differently.

### Managed connectors (Gmail, Google Calendar, Google Drive)

These ride your Claude.ai subscription and are **enabled on the Claude.ai web app, not
in the CLI** — they're account-level, so enabling one once makes it available to any
Claude Code session logged in as that account.

1. In a browser (any machine), open **claude.ai → Settings → Connectors**
   (<https://claude.ai/settings/connectors>). Browse the available connectors and
   complete each one's OAuth sign-in.
2. Back in the `claude` shell on the server, confirm they loaded:

   ```bash
   /mcp        # lists connected servers; managed ones show a `claude.ai` prefix
   /status     # confirm Claude.ai subscription auth (not API key) if none appear
   ```

   `/mcp` is **view-only** for managed connectors — you can't toggle them here.
   (Some Anthropic-hosted connectors can't OAuth from the CLI at all; the shell just
   redirects you to the web Settings above.)

3. **Classify each connector in the trust gate.** The gate ships a preset catalog in
   `trust/config/connector-legs.toml` (see `trust/README.md` → "Connector presets"):
   Gmail, Calendar, Drive, Todoist, Dropbox are **active** out of the box; a dozen
   directory connectors (Slack, Notion, GitHub, …) ship **commented** — enable the ones
   you use by uncommenting their block and fixing the `match` patterns to the real names
   `/mcp` shows. Then run **`make trust-check`** — it confirms the config parses, flags
   any connector left half-classified (a `mcp__…` rule with zero legs), and **diffs your
   rules against the live connected servers** (`claude mcp list`): a connected server
   matching *no* rule is a hard failure (its calls all fall to the unknown-tool ask path
   and its real legs never apply — the check names the commented block to uncomment when
   one exists); a rule matching no connected server is flagged as a dead pattern. No
   `claude` on PATH? It degrades to the config-only checks; you can also feed it pasted
   names: `.venv/bin/python scripts/trust_check.py --names-file <file>`. The diff is
   server-level — when enabling a connector, still cross-check its full `/mcp` tool list
   for send-override exhaustiveness (the STANDING RULE in `connector-legs.toml`), and
   optionally run the substrate check in `trust/tests/smoke_substrate.md` step 5.

### Local MCP connectors — expert path, at your own risk

> **At your own risk.** Local MCP servers run as your buzai user with whatever
> credentials you give them, and they are **not vetted by Anthropic**. You own their
> security, scopes, and supply chain. This section assumes you're already comfortable
> setting up MCP servers; beyond the example below it's DIY.

Register a self-hosted MCP server with `claude mcp add`. Example — a Google Sheets
server that needs a Google OAuth token with Sheets scopes:

```bash
claude mcp add --scope user --transport stdio sheets \
  --env GOOGLE_OAUTH_TOKEN='${GOOGLE_OAUTH_TOKEN}' \
  -- npx <a-sheets-mcp-server>
```

Other examples an adopter might add the same way: an **IMAP/SMTP** mailbox server, or
any stdio/HTTP MCP server you trust. Manage with `claude mcp list`, `claude mcp get
sheets`, `claude mcp remove sheets`.

**Keep secrets out of config.** `claude mcp add --env` stores the value in
`~/.claude.json`; instead reference `${GOOGLE_OAUTH_TOKEN}` (as above) and supply the
real value from your secrets env-file at `~/.config/buzai/secrets/*.env` (mode 0600,
outside the repo), injected into the service environment by the systemd unit — see
`deploy/README.md` step 1.

> **Instructive (not required):** for the Sheets example, the `claude` shell itself can
> walk you through creating the Google Cloud app and OAuth token with the exact Sheets
> scopes needed for edit access — just ask it in-session. It's a handy way to get the
> token; the MCP server you point it at is still your choice.

## 5. Wire the trust gate

**What it is:** the trust gate is buzai's single safety choke point — a PreToolUse
hook that intercepts *every* tool call (connectors and subagent calls included),
checks it against your trust tiers, and records it to an append-only, tamper-evident
audit log. It's what keeps an always-on assistant acting on your accounts bounded.
Wire it before seeding hubs, so the gate is proven before the assistant touches
anything. `trust/README.md` has the full (contributor-level) detail; this is the
no-brainer setup.

The two sub-steps below are each one `make` verb. They verify differently: (a)
self-verifies through a visible test run, while (b) ends in a check *you* perform
against the audit log. (This bootstrap runs on an un-gated session — that's fine and
unavoidable: you're the owner, attended, setting up the gate that bounds everything
afterward.)

**a. Give it a self-contained Python.** The gate is zero-dependency, but it needs
Python 3.11+, and relying on whatever the host happens to ship is the classic adopter
footgun — a fresh Linux box can easily ship a system Python *below* 3.11 (we hit
exactly this: a clean host with system Python 3.10). So pin your own interpreter with
`uv`. No security surface here yet, and it self-verifies by running the tests:

```bash
make venv     # installs uv if needed, creates the pinned .venv (CPython per .python-version)
make test     # runs the trust unit + gate suite under that .venv — expect OK
```

`.venv/` is gitignored (per-host); the pinned version lives in the tracked
`.python-version`. The hook, the systemd preflight, and the helper scripts all run
through `.venv/bin/python` — never the host python.

**b. Wire and self-test the gate.**

```bash
make trust-install     # merges the hooks into .claude/settings.json + locks audit/ to 0700
```

`trust-install` is a deterministic, **idempotent** merge (safe to re-run; it never
overwrites your settings, and hard-fails rather than clobbering a malformed one). It
registers two hooks: a `PreToolUse` hook (`trust/bin/gate`) that gates every tool call,
and a `UserPromptSubmit` hook (`trust/bin/mark-turn`) that marks each turn's provenance
as `owner` so the gate can resolve your owner tier — both exec the `.venv` Python from
(a). It also sets `audit/` to **0700**, owned by you, so no ordinary tool call can
rewrite the audit log. Then:

- No `tiers.local.toml` edit is needed for the owner case — the shipped `tiers.toml`
  maps the `owner` kind the writer emits to the owner tier. Add channel mappings only
  when you later wire automation or known-contact sources (see
  `trust/config/tiers.local.toml.example`).
- **Restart the Claude Code session** — Claude Code snapshots hook config at session
  start, so the hooks you just added do **not** activate until you restart (a
  mid-session `settings.json` write is inert). Until you restart, `audit/audit.jsonl`
  stays empty because no real tool call is being gated.
- **Verify it's live.** After restarting, ask the assistant to do one small thing (e.g.
  *"list my calendars"*), then run **`make audit`** — you should see that action logged
  with `tier=OWNER`. That's the gate intercepting every tool call. *(Want the thorough
  per-connector + subagent check under the hood? That's `trust/tests/smoke_substrate.md`,
  and it's optional.)*

**Verify against the audit log, not the assistant's word.** Because `audit/` is 0700
and unwritable by an ordinary tool call, the append-only `audit/audit.jsonl` is a
record the assistant can't forge. A correctly wired owner turn shows `tier=OWNER` in
its entries; when you confirm the gate *asks* or *denies* for something it should
(smoke-test SC5), read that log yourself (`make audit`) rather than trusting a "✅
blocked" reply — a misbehaving assistant would lie about its own leash.

## 6. Start the service

Robust always-on path (Linux) — the `--user` systemd unit. **Most adopters need no
secrets for this step:** if all your connectors are claude.ai-managed (Gmail, Calendar,
Drive, Todoist, Dropbox, …), their OAuth lives in `~/.claude/.credentials.json` and the
unit needs **no** `EnvironmentFile` — leave those lines commented (their default). Only
**local MCP** connectors need the secrets step in `deploy/README.md`.

> **Prime Remote Control once, interactively — REQUIRED, and the #1 onboarding trap.**
> `claude remote-control` has first-run prompts a headless service **can't answer**.
> Until you complete them once in a terminal, the service starts `active` but
> **silently never registers** — nothing appears at claude.ai/code, and
> `scripts/liveness.py` reports NOT-LIVE (0 relay sockets). Run it once as the buzai
> user and answer both prompts:
>
> ```bash
> make prime-consent                          # runs `claude remote-control --name <NAME>`
> #   "Enable Remote Control? (y/n)"  -> y
> #   "Spawn mode for this project [1/2]" -> 1   (same-dir)
> #   then confirm "buzai-assistant" shows up at claude.ai/code (or mobile Code tab),
> #   and exit with Ctrl-D Ctrl-D  (claude exits on EOF/Ctrl-C, not SIGTERM)
> ```
>
> These choices persist per-project (`~/buzai`). The unit passes `--spawn=same-dir`, so
> after this only the one-time **"Enable Remote Control?"** consent matters (it has no
> CLI flag to skip). Requires full-scope subscription auth from §3 — an inference-only
> `setup-token` runs interactive sessions but **cannot register** the remote session.

**Then install and enable the unit:**

```bash
make service-install      # installs the --user unit (pass NAME= / WORKDIR= if your clone isn't ~/buzai)
make service-start        # systemctl --user enable --now  (start now AND on boot)
loginctl enable-linger "$USER"   # keep running without an active login (may need sudo)
```

It runs the **long-lived** remote-control server (sessions attach on demand; not
`--spawn session`, which swallows prompts during respawn), restarts on crash, and logs
to journald. The unit sets `KillSignal=SIGINT` so `systemctl stop/restart` is prompt
(claude exits on Ctrl-C/SIGINT, *ignores* SIGTERM — without this, stop hangs ~90s). Full
detail + the local-MCP secrets path: `deploy/README.md`.

**Quick / local / non-Linux** alternative — run it in the foreground and watch output:

```bash
claude remote-control --name buzai-assistant
```

(Foreground is fine for a quick test, but it stops when your shell closes; use the
systemd unit for always-on.)

## 7. Attach & reconnect

Attach from the **Claude mobile app → Code tab** or **claude.ai/code** in a browser,
signed into the **same** Claude account — your running `buzai-assistant` session appears
there automatically (account-based discovery). This is the normal path; there is no
QR/URL to scan for it.

A per-session deep-link is also emitted (`claude.ai/code?environment=env_…`) if you want
to jump straight to the session. Retrieve it from the service logs:

```bash
make env-url   # owner-equivalent deep-link — display only, never log/commit/share
```

`make env-url` only prints to an interactive terminal — it **refuses to write to a
redirect or pipe**, so this owner-equivalent link can't land in a file or log by
accident. **After any restart the environment id rotates**, so a stale deep-link stops
working — just reopen claude.ai/code (discovery always finds the current session) or
re-run `make env-url`.

> **Caveat:** the deep-link parse is **best-effort and CLI-version-dependent** — it
> scrapes the URL from journald and the exact log line isn't pinned across versions, so on
> some builds `env_url.py` may print "no environment URL found". The **account-based path
> (claude.ai/code / mobile Code tab) is the validated, version-independent way to attach**;
> treat the deep-link as a convenience.

**Reauth is rare.** A normal restart or reboot needs no re-login — the Claude OAuth and
relay tokens in `~/.claude/.credentials.json` persist and re-mint on a bounce. You only
re-authenticate when: the relay token lapses after a very long uptime (recovery: bounce
the service); the **host clock drifts** (skew breaks token validation — keep NTP synced);
or a managed connector's one-time attended OAuth was never completed or has expired (re-do
it in the Claude.ai web app — managed-connector OAuth **cannot** be (re)established
headlessly).

## 8. Liveness & self-diagnosis

Health is the **remote-control process holding an established relay socket** (an
`ESTAB :443` owned by its PID), NOT the "Ready" log banner (often never emitted). An
always-on assistant is legitimately idle, so a recent turn is *informational*, not
required for LIVE — `liveness.py` reports LIVE on a connected-but-idle session and flags
stale/absent turns without failing.

```bash
make liveness   # LIVE / NOT-LIVE
make doctor     # aggregate: prereqs + venv + secrets-preflight + liveness, in one shot
```

`make doctor` is the fastest "is everything OK?" — it runs the common checks together.
When you need to dig deeper, the manual self-diagnosis steps (carried from real
operation):

1. Unit active? `systemctl --user is-active claude-remote`
2. Relay socket established? `ss -tnp | grep <pid>` → `ESTAB …:443`
3. Token expiries in the future? (check `~/.claude/.credentials.json`)
4. Model resolves to an available model?
5. MCP servers connected? (some need interactive auth, absent headless)
6. Recent restart loop? `journalctl --user -u claude-remote`
7. Host basics: load, memory, disk, **clock** (skew breaks token validation).

**Known red herrings — don't chase these:** `statsig.anthropic.com` not resolving
(no public record by design); client scroll lag on a small transcript (client-side
render of large tool output); a phantom "running" task with no process (orphan from a
dead session).

## 9. Hubs

With the assistant running and attached, seed your knowledge base. This is a
**post-setup step you can drive entirely from the Claude app over Remote Control** — ask
the assistant to create and populate hubs in the running session — or edit the files
directly on the box. Start from the shipped scaffolds (see `hubs/README.md`); your
populated hubs are personal and gitignored, only the scaffolds are tracked.

## 10. Verify

Run `make smoke` (the SC0 preflight gate), then complete the 8-point smoke test:
`docs/SMOKE-TEST.md`. Your instance is "stood up and working" when all eight pass.

## Updating later — don't wipe your wired state

When you pull a new version of buzai, **update in place — do not `rm -rf` and
re-clone.** A fresh clone only restores *tracked* files; it destroys the untracked
local state your running instance depends on:

- `.claude/settings.json` — your wired trust-gate hooks (the repo ships only
  `trust/settings.example.json`; the live file is yours, from `make trust-install`)
- `audit/` — the trust-gate audit log
- `.venv/` — the pinned interpreter (a costly rebuild)
- `.buzai/` — provenance + accumulated gate state
- `trust/config/*.local.toml` and your populated `hubs/`

Update in place instead. Normal case — as the `buzai` user, pull directly:

```bash
git -C ~/buzai pull --ff-only origin main     # only touches TRACKED files
```

*Fallback* — updating from your `sudo` account instead (you're not in a `buzai`
session right now): run git **as `buzai`**, never as root — root-run git inside a
buzai-writable repo can execute buzai-controlled hooks/config as root, and it
leaves root-owned files behind:

```bash
# sudo account — git runs as buzai: no safe.directory entry, no chown-after needed
sudo -u buzai git -C /home/buzai/buzai pull --ff-only origin main   # only touches TRACKED files
```

Either way, `git pull` leaves all the untracked state above intact, so your gate stays
wired and your audit log continues. (If you genuinely must re-clone, copy out
`.claude/settings.json`, `audit/`, `trust/config/*.local.toml`, and `hubs/` first, then
restore them.) After an update, **restart the Claude Code session** so any changed hook
config takes effect — and re-run `make test` if the gate code changed.

## Security

The always-on session is bound to **your** Claude.ai account: attachment is
**account-gated** — anyone signed into your Claude account (claude.ai/code or the mobile
Code tab) can attach and thereby reach every connector and your personal hubs. So your
Claude account credentials are the real perimeter; protect that login (and use its
2FA). The per-session deep-link (`?environment=env_…`, via `scripts/env_url.py`) is
**owner-equivalent** — treat it as a secret: never log, share, or commit it. Secrets
live outside the repo at 0600; the credentials file is 0600; the trust gate audit log is
append-only and 0700.

## Scope — what this is and isn't (yet)

In: survives-restart always-on, connectors + hubs + the trust gate, the smoke test,
and a `make` front door for setup/operate (including `make doctor`, the health
aggregate). Out (deferred): a token-expiry watchdog, orphaned-task reaper,
model-availability fallback, Docker, a hosted/multi-tenant offering, a GUI installer.

## Known rough edges

- The exact journald line carrying the environment URL (finalize `scripts/env_url.py`'s
  pattern from a real sample), and the relay peer + last-good-turn marker for
  `scripts/liveness.py`.
- Whether `claude setup-token`'s long-lived `CLAUDE_CODE_OAUTH_TOKEN` loads managed
  connectors, or only interactive subscription login does (step 3 assumes the latter).

> Resolved during setup: headless login needs **no** SSH-forwarded callback — Claude
> Code falls back to the paste-the-code flow automatically (step 3).
