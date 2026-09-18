# Host bootstrap — provision a dedicated user account

**Step 0, before [`docs/QUICKSTART.md`](QUICKSTART.md) / [`SETUP.md`](SETUP.md).**
This provisions a dedicated, unprivileged Linux user to host one buzai instance.
It's OS-level (runs as root) and the same on any Debian/Ubuntu/RHEL-family host.

## The one-liner

From any sudo-capable account on the host:

```bash
curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | sudo bash
```

(Prefer to read first? `install.sh` is short, plain bash, and lives at the repo root.
From a clone, `make bootstrap` runs the same thing. Override the account name with
`BUZAI_USER=<name>`.)

The root phase is **idempotent** — re-running it changes nothing already in place.
It is also the **only time root is used**: everything after it, setup and normal
operation alike, runs inside the unprivileged `buzai` account. It:

1. **Creates the `buzai` account** (`useradd -m -s /bin/bash`; no sudo, no extra
   groups, password left **locked** on purpose — access is by ssh key or `sudo -u`,
   never a password).
2. **Enables linger** (`loginctl enable-linger`) — lets the account's `systemctl --user`
   services start at boot and keep running after logout; what makes "always-on" real.
3. **Wires `XDG_RUNTIME_DIR` into the account's login init file** — so the `sudo -u buzai -i`
   fallback path can reach the per-user systemd bus (*"Failed to connect to bus"*,
   the classic trip-up; a real ssh login gets this from `pam_systemd` natively).
   The target is the **first existing** of `.bash_profile` / `.bash_login` /
   `.profile` — bash login shells read only the first one, and Debian skel ships
   `.profile` while RHEL-family skel ships `.bash_profile` — never `.bashrc`: the
   stock `.bashrc` returns early in non-interactive shells, so a line there
   silently never runs for scripted logins.
4. **Copies your `authorized_keys` to the account** — so `ssh buzai@host` works
   directly, which is the primary path for setup and every day-2 session. Key-only
   (the password stays locked), and access-equivalent: anyone holding those keys
   already has your sudo. The copy is a **point-in-time snapshot**: revoking a key
   on your admin account later does *not* revoke its buzai access — remove it from
   `/home/buzai/.ssh/authorized_keys` too. Opt out with the var after `sudo`:
   `curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | sudo BUZAI_COPY_SSH_KEYS=0 bash`;
   skipped automatically when the invoking account has no `authorized_keys`.
5. **Sets a default git identity for the account** (`~/.gitconfig`, `name = buzai`,
   `email = buzai@localhost`) — your hub store is a git repository and `make hub-init`
   refuses to commit without a committer identity, which a fresh account does not have.
   Skipped if one is already set, and safe to change any time: it is only a fallback,
   because every hub write stamps the assistant's own author on the commit.
6. **Verifies both failure modes separately**: (a) the per-user systemd *manager* runs
   (linger problem if not), and (b) a fresh *login* is correctly wired (login-file
   problem if not). It fails loudly naming which one broke.

## Why a dedicated account

One buzai instance = one Linux user = one principal. A dedicated account **isolates**
the assistant's workspace, hubs, secrets, and Claude credentials from your personal
account — the trust gate's blast radius stops at this user — and gives a clean home for
`~/.claude/.credentials.json`, `~/.config/buzai/secrets/`, and `~/buzai`.

## Next: log in as `buzai` and run the user phase

```bash
# from your workstation — bootstrap copied your key to the account
ssh buzai@<host>
curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | bash
```

No key on the account (password-auth admin, opted out, or a restrictive
`sshd_config` `AllowUsers`/`AllowGroups`)? Switch in from your sudo account
instead — it always works:

```bash
sudo -u buzai -i
curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | bash
```

Either way the user phase fetches the repo into `~/buzai` and hands off to
`make setup` (see [`SETUP.md`](SETUP.md)). It refuses to run inside a
sudo-capable account (the forgot-`sudo`-in-step-1 footgun) — override with
`BUZAI_ALLOW_ADMIN_INSTALL=1` only if you truly mean to.

## Heads-up: Claude.ai auth is per-account

Managed connectors (Gmail/Calendar/Drive/Todoist) load only under **Claude.ai
subscription auth**, and that auth lives in **this account's own**
`~/.claude/.credentials.json`. A fresh account starts with none — `make setup` pauses
at the login step; inside the buzai account you either `/login` as yourself (the
instance shares your subscription) or copy in a credentials file you established
elsewhere (see `SETUP.md`).

## Manual path (what the script does, by hand)

If you'd rather run the steps yourself:

```bash
sudo useradd -m -s /bin/bash buzai
sudo loginctl enable-linger buzai
# append to the account's LOGIN init file — bash reads only the first existing of
# .bash_profile / .bash_login / .profile, so on a RHEL-family host (whose skel ships
# .bash_profile and no .profile) target /home/buzai/.bash_profile instead
sudo -u buzai tee -a /home/buzai/.profile >/dev/null <<'EOF'

# wire the per-user systemd bus for `systemctl --user`
export XDG_RUNTIME_DIR=/run/user/$(id -u)
EOF

# ssh access — copy your keys so `ssh buzai@host` works (key-only; password stays
# locked). Append rather than install, so an existing authorized_keys is preserved —
# and remember the copy is a snapshot: revoke keys on the buzai account separately.
sudo install -d -m 700 -o buzai -g buzai /home/buzai/.ssh
sudo tee -a /home/buzai/.ssh/authorized_keys <~/.ssh/authorized_keys >/dev/null
sudo chown buzai:buzai /home/buzai/.ssh/authorized_keys
sudo chmod 600 /home/buzai/.ssh/authorized_keys

# git identity — the hub store is a git repo and `make hub-init` will not commit
# without one. Only a fallback: hub writes carry the assistant's own author.
sudo -u buzai tee -a /home/buzai/.gitconfig >/dev/null <<'EOF'

[user]
	name = buzai
	email = buzai@localhost
EOF

# verify (a) — manager up (tests linger, not your login env):
sudo -u buzai XDG_RUNTIME_DIR=/run/user/$(id -u buzai) systemctl --user is-system-running   # → running
# verify (b) — fresh login wired (proves the .profile write landed):
sudo -u buzai -i bash -c 'echo "$XDG_RUNTIME_DIR"; systemctl --user is-system-running'      # → non-empty + running
```

If (a) runs but (b) says *"Failed to connect to bus"*, the login-file write is missing
for this user (or landed in a file the login shell never reads — check which of
`.bash_profile`/`.bash_login`/`.profile` exists) — standing up a *second* account is
the usual place it gets skipped.
