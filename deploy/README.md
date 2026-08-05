# Deploy: running buzai as an always-on `--user` systemd service

This is the robust always-on path for a Linux server. For a quick local or
non-Linux run, see the manual CLI invocation in `docs/SETUP.md`.

## 1. Place secrets outside the repo (0600) — *local MCP connectors only*

> **Managed-connector-only?** If all your connectors are claude.ai-managed
> (Gmail/Calendar/Drive/Todoist/Dropbox/etc.), you have **no local secrets** —
> their OAuth lives in `~/.claude/.credentials.json`. **Skip this whole step**,
> leave the `EnvironmentFile=` lines in the unit commented (their default), and go
> to step 2. `secrets_preflight.py` treats "no local secret files" as fine — it fails
> on an unsafe *layout* (loose perms, a tracked secret, personal state inside the
> checkout), never on the mere absence of secrets — so the service starts cleanly.

**`~/.config/buzai/secrets/*.env` is the only place real values live.** Not "the
preferred place" — the only one. `deploy/env/` inside the checkout holds **tracked
`*.env.example` scaffolds and nothing else**, and `secrets_preflight.py` now **fails
service start** if it finds a real `deploy/env/*.env` there, naming the file. That is
deliberate: a secret committed once survives its own deletion, and in a public repo it
is world-mirrored and cannot be un-published, so `.gitignore` alone is not a barrier
worth resting on. Secrets are the never-versioned tier — they belong at a path with no
git repository anywhere above them (see `docs/decisions/private-versioned-hubs.md`).

Upgrading an instance that already has real values under `deploy/env/`? One-time move,
then confirm:

```bash
mkdir -p ~/.config/buzai/secrets && chmod 700 ~/.config/buzai/secrets
mv deploy/env/*.env ~/.config/buzai/secrets/     # NOT the *.env.example scaffolds
chmod 600 ~/.config/buzai/secrets/*.env
make doctor                                      # must report secrets-preflight OK
```

Only if you run **local MCP connectors** (e.g. a local Sheets/IMAP/SMTP server),
their secrets must live **outside the workspace tree**, so the assistant's own
file-write tools can't reach them:

```bash
mkdir -p ~/.config/buzai/secrets && chmod 700 ~/.config/buzai/secrets
cp deploy/env/email.env.example ~/.config/buzai/secrets/email.env
cp deploy/env/xero.env.example  ~/.config/buzai/secrets/xero.env
chmod 600 ~/.config/buzai/secrets/*.env
# edit each file, replace every CHANGEME
```

Then **uncomment the matching `EnvironmentFile=` line(s)** in the unit (step 2).
The managed-connector OAuth credentials live separately at
`~/.claude/.credentials.json` (also 0600 — see `docs/SETUP.md`).

### The hub remote credential — an ssh deploy key, never a token in a URL

The private hub repo (`make hub-init`) pushes off-box with a credential that must
reach **that one repository and nothing else**. Use an **ssh deploy key scoped to
the hub repo**, with write access enabled:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/buzai-hub -N '' -C "buzai hub deploy key"
chmod 600 ~/.ssh/buzai-hub
# add ~/.ssh/buzai-hub.pub to the HUB repo only: Settings -> Deploy keys ->
# "Add deploy key", tick "Allow write access". Never as an account-wide SSH key.

# pre-populate known_hosts so a headless push can never hit a host-key prompt
ssh-keyscan github.com >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts

cat >> ~/.ssh/config <<'EOF'
Host buzai-hub
  HostName github.com
  User git
  IdentityFile ~/.ssh/buzai-hub
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config

git -C ~/hubs remote set-url origin buzai-hub:<owner>/<hub-repo>.git
```

The key goes in `~/.ssh/config` as a `Host` alias, **not** in the repo's
`core.sshCommand`: the privacy check sets `GIT_SSH_COMMAND` (to force
`BatchMode=yes`), and that environment variable overrides `core.sshCommand`, so a
deploy key configured there would be silently dropped. `known_hosts` must be
populated **before** the first push — every git invocation on this path runs
non-interactively (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`), so an unknown
host key fails fast rather than hanging a TTY-less service.

The key is a secret like any other, and the preflight treats it as one: `~/.ssh/buzai-hub`
must be **0600** and **untracked by any git repo** — a dotfiles repo at `~` that tracks it
publishes write access to your knowledge base. Both are fatal at service start. The path is
not configurable; a second supported location would be a second thing to check and a second
thing to get wrong.

**A token embedded in the remote URL is forbidden.** Not discouraged — forbidden,
and `scripts/hub_remote.py` refuses to push when it sees one. Git echoes the
remote URL on failure and the unit sends stderr to the journal, so
`https://user:TOKEN@host/…` would be written to the logs in plaintext on exactly
the push failures this design expects routinely (fine-grained tokens expire, so
silent push failure is a steady state, not an edge case).

Before anything is pushed, prove the destination is private:

```bash
make hub-remote-check
```

It runs an **anonymous** `git ls-remote` — all credentials suppressed — against
the remote. If that read *succeeds*, the repo is publicly readable and the push is
refused. Unreachable, timed out or otherwise inconclusive also refuses: the check
distinguishes public / private / indeterminate and never assumes private. The
verdict is cached inside the hub repo at `.git/buzai/remote-verified.json` with a
one-hour TTL and re-verified at service start, so a repo flipped to public later —
same clone URL — is caught rather than trusted forever.

## 2. Install the unit

```bash
mkdir -p ~/.config/systemd/user
cp deploy/claude-remote.service.template ~/.config/systemd/user/claude-remote.service
# edit WorkingDirectory / paths / --name if your clone isn't at ~/buzai
systemctl --user daemon-reload
systemctl --user enable --now claude-remote
```

The unit runs the **long-lived** `claude remote-control` server (sessions attach
on demand) — not `--spawn session`, which exits on completion and, with
`Restart=always`, swallows prompts during the respawn window. The
`EnvironmentFile=` lines ship **commented** (a managed-only setup needs none); any
you **uncomment** have **no** `-` prefix, so a missing secrets file makes the unit
fail to start (fail-closed). The commented `Environment=BUZAI_HUBS_DIR=` line is where
a non-default hub location goes — a `--user` unit sources no shell profile, so setting
it only in `~/.profile` would point the assistant at a different store than the one you
inspect by hand.

`ExecStartPre` runs `scripts/secrets_preflight.py`, which prints the hub path it
resolved and then splits its findings two ways:

- **`FAIL:` — leak conditions block start.** Loose perms or a git-tracked secret (as
  before), plus: a real `.env` under `deploy/env/`, personal hub content inside the
  checkout's `hubs/`, a hub path inside or containing the checkout, a hub deploy key
  that is not 0600 or is tracked, and a credential helper configured for the **public**
  origin.
- **`WARN:` — durability conditions exit 0 and never block start.** A hub store with no
  remote past its grace period, unpushed commits, a dirty hub working tree, a git repo
  nested above the hub directory. With `StartLimitBurst=5` above, failing on those would
  turn "knowledge is not backed up" into "the assistant is gone".

It passes cleanly when there are simply no local secrets and no hub store yet.

## 3. Survive reboot (linger)

```bash
loginctl enable-linger "$USER"   # usually needs sudo / a polkit grant
```

Without linger, a `--user` service stops at logout and does not start on boot.
If you can't enable linger (no privilege), fall back to a system-level unit or
ask your admin — note the assistant won't be always-on until this is set.

## 4. Logs

```bash
journalctl --user -u claude-remote -f                  # follow
.venv/bin/python scripts/env_url.py             # current reconnect URL (from logs)
.venv/bin/python scripts/liveness.py            # ready / not-ready
```

After any restart the relay environment id rotates — reconnect from the Claude
app to the **new** URL (`scripts/env_url.py`); the old window silently drops input.
