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
3. **Wires `XDG_RUNTIME_DIR` into the account's `.profile`** — so the `sudo -u buzai -i`
   fallback path can reach the per-user systemd bus (*"Failed to connect to bus"*,
   the classic trip-up; a real ssh login gets this from `pam_systemd` natively).
   `.profile`, not `.bashrc`: the stock `.bashrc` returns early in non-interactive
   shells, so a line there silently never runs for scripted logins.
4. **Copies your `authorized_keys` to the account** — so `ssh buzai@host` works
   directly, which is the primary path for setup and every day-2 session. Key-only
   (the password stays locked), and access-equivalent: anyone holding those keys
   already has your sudo. Opt out with `BUZAI_COPY_SSH_KEYS=0`; skipped
   automatically when the invoking account has no `authorized_keys`.
5. **Verifies both failure modes separately**: (a) the per-user systemd *manager* runs
   (linger problem if not), and (b) a fresh *login* is correctly wired (`.profile`
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
sudo -u buzai tee -a /home/buzai/.profile >/dev/null <<'EOF'

# wire the per-user systemd bus for `systemctl --user`
export XDG_RUNTIME_DIR=/run/user/$(id -u)
EOF

# ssh access — copy your keys so `ssh buzai@host` works (key-only; password stays locked)
sudo install -d -m 700 -o buzai -g buzai /home/buzai/.ssh
sudo install -m 600 -o buzai -g buzai ~/.ssh/authorized_keys /home/buzai/.ssh/authorized_keys

# verify (a) — manager up (tests linger, not your login env):
sudo -u buzai XDG_RUNTIME_DIR=/run/user/$(id -u buzai) systemctl --user is-system-running   # → running
# verify (b) — fresh login wired (proves the .profile write landed):
sudo -u buzai -i bash -c 'echo "$XDG_RUNTIME_DIR"; systemctl --user is-system-running'      # → non-empty + running
```

If (a) runs but (b) says *"Failed to connect to bus"*, the `.profile` write is missing
for this user — standing up a *second* account is the usual place it gets skipped.
