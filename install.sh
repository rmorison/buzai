#!/usr/bin/env bash
#
# buzai installer — one script, two phases, picked by who runs it.
#
#   ROOT phase (host bootstrap; run with sudo on a fresh host):
#       curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | sudo bash
#     Creates the dedicated unprivileged user (default: buzai), enables linger,
#     wires XDG_RUNTIME_DIR into its login init file, copies the invoking admin's
#     authorized_keys so `ssh buzai@host` works directly (opt out:
#     BUZAI_COPY_SSH_KEYS=0), and verifies the per-user systemd manager — the
#     steps documented in docs/HOST-BOOTSTRAP.md. Idempotent: re-running
#     changes nothing that's already in place.
#
#   USER phase (run inside the service account, after `sudo -u buzai -i`):
#       curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | bash
#     Fetches the repo into ~/buzai (git clone, or tarball when git is absent)
#     and hands off to `make setup`, which drives the rest and pauses only at
#     the inherently manual steps (auth login, connector OAuth, consent prime).
#
# Prefer to read before you run? The whole flow is plain `make` verbs — clone the
# repo and run `make bootstrap` (root steps) / `make setup` (user steps) yourself.
#
# Options (env vars):
#   BUZAI_USER=<name>              service account for the root phase   (default: buzai)
#   BUZAI_REPO=<url>               repo to fetch in the user phase      (default: https://github.com/rmorison/buzai)
#   BUZAI_WORKDIR=<dir>            workspace for the user phase         (default: $HOME/buzai)
#   BUZAI_COPY_SSH_KEYS=0          root phase: don't copy the admin's authorized_keys
#   BUZAI_ALLOW_ADMIN_INSTALL=1    user phase: allow install into a sudo-capable account
#
# Update/upgrade of an existing install is out of scope for now: the user phase
# refuses to overwrite an existing non-empty workspace that isn't a git checkout.
set -euo pipefail

BUZAI_USER="${BUZAI_USER:-buzai}"
BUZAI_REPO="${BUZAI_REPO:-https://github.com/rmorison/buzai}"
BUZAI_WORKDIR="${BUZAI_WORKDIR:-$HOME/buzai}"

say()  { printf '%s\n' "$*"; }
fail() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# Root-phase writes into the service home must never follow a planted symlink:
# touch/chmod/chown/append all dereference links, so re-running bootstrap against
# a pre-existing hostile account could redirect a root write to an arbitrary
# target. Refuse loudly — simple and auditable — rather than write around it.
refuse_symlink() {
  if [ -L "$1" ]; then
    fail "$1 is a symlink — refusing: the root phase writes here as root and would follow the link to its target. Remove the link and re-run."
  fi
}

# Guard the #1 footgun: installing the stack into a sudo-capable account defeats
# the isolation the dedicated user exists for. Test capability first (`sudo -n -l`
# catches sudoers.d and google-sudoers grants that a group-name test misses), then
# fall back to admin group names (a password-required sudo grant makes `sudo -n`
# exit nonzero even though the account has rights). Exact-line match, not grep -w:
# '-' is a word boundary, so -w would false-positive on e.g. an 'admin-users' group.
refuse_admin_account() {
  if [ "${BUZAI_ALLOW_ADMIN_INSTALL:-0}" = "1" ]; then
    return 0
  fi
  if sudo -n -l >/dev/null 2>&1 || id -nG | tr ' ' '\n' | grep -qxE 'sudo|wheel|admin|google-sudoers'; then
    fail "account '$(id -un)' is sudo-capable — refusing to install buzai into it.
buzai belongs in its own unprivileged account. You probably meant one of:

    curl -fsSL ${BUZAI_REPO}/raw/main/install.sh | sudo bash    # bootstrap (note: sudo)
    ssh ${BUZAI_USER}@<host>   # then install there: re-run the one-liner, or
                               # git clone + make setup (or: sudo -u ${BUZAI_USER} -i)

Really install into '$(id -un)'? Re-run with BUZAI_ALLOW_ADMIN_INSTALL=1."
  fi
}

# ------------------------------------------------------------------ root phase
root_phase() {
  local user="$1" home uid

  say "== buzai host bootstrap (root phase) — service account: $user =="

  # 1a. account — password stays LOCKED by design (you switch in via sudo, never log in)
  if id -u "$user" >/dev/null 2>&1; then
    say "   account '$user' exists — skipping useradd"
  else
    useradd -m -s /bin/bash "$user"
    say "   created account '$user' (password locked)"
  fi
  home="$(getent passwd "$user" | cut -d: -f6)"
  uid="$(id -u "$user")"

  # A pre-existing home not owned by the service user is a hostile-account signal —
  # every write below lands inside it as root, so refuse rather than proceed.
  if [ "$(stat -c %U "$home" 2>/dev/null)" != "$user" ]; then
    fail "home directory $home is not owned by '$user' — refusing to write into it; fix ownership and re-run"
  fi

  # 1b. linger — lets `systemctl --user` services start at boot and survive logout
  if [ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null)" = "yes" ]; then
    say "   linger already enabled"
  else
    loginctl enable-linger "$user"
    say "   linger enabled"
  fi

  # 1c. persist XDG_RUNTIME_DIR — without it the sudo -u path can't reach the per-user
  #     systemd bus ("Failed to connect to bus", the classic trip-up). Target the login
  #     init file, NOT ~/.bashrc: the stock skel .bashrc returns early for non-interactive
  #     shells, so a .bashrc line never runs for `su -l -c`/scripted logins. Bash login
  #     shells read only the FIRST existing of .bash_profile/.bash_login/.profile —
  #     Debian skel ships .profile, RHEL-family skel ships .bash_profile and no .profile —
  #     so pick that first existing file (fall back to .profile on a bare home). Quoted
  #     heredoc keeps $(id -u) literal so it evaluates at each login.
  local login_file="$home/.profile" f
  for f in "$home/.bash_profile" "$home/.bash_login" "$home/.profile"; do
    if [ -e "$f" ]; then login_file="$f"; break; fi
  done
  refuse_symlink "$login_file"
  if grep -q 'XDG_RUNTIME_DIR=/run/user' "$login_file" 2>/dev/null; then
    say "   XDG_RUNTIME_DIR already wired in ${login_file##*/}"
  else
    tee -a "$login_file" >/dev/null <<'EOF'

# wire the per-user systemd bus for `systemctl --user`
export XDG_RUNTIME_DIR=/run/user/$(id -u)
EOF
    chown "$user:$user" "$login_file"
    say "   XDG_RUNTIME_DIR wired into ${login_file##*/}"
  fi

  # 1d. ssh access — copy the invoking admin's authorized_keys so `ssh buzai@host`
  #     works directly (key-only; the password stays locked, so this adds no access
  #     the admin's sudo didn't already grant). Direct ssh is the primary day-2 path:
  #     a real login session gets XDG_RUNTIME_DIR from pam_systemd natively.
  local ssh_ready="" admin_home admin_keys
  if [ "${BUZAI_COPY_SSH_KEYS:-1}" != "1" ]; then
    say "   ssh key copy: skipped (BUZAI_COPY_SSH_KEYS=${BUZAI_COPY_SSH_KEYS}) — switch in with: sudo -u $user -i"
  elif [ -z "${SUDO_USER:-}" ] || [ "$SUDO_USER" = "$user" ] || [ "$SUDO_USER" = "root" ]; then
    say "   ssh key copy: no invoking admin to copy from (direct root shell?) — switch in with: sudo -u $user -i"
  else
    admin_home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    admin_keys="$admin_home/.ssh/authorized_keys"
    if [ -s "$admin_keys" ]; then
      refuse_symlink "$home/.ssh"
      refuse_symlink "$home/.ssh/authorized_keys"
      install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
      touch "$home/.ssh/authorized_keys"
      # append only keys not already present, so re-runs stay idempotent
      while IFS= read -r key || [ -n "$key" ]; do
        case "$key" in ''|\#*) continue ;; esac
        grep -qxF "$key" "$home/.ssh/authorized_keys" \
          || printf '%s\n' "$key" >>"$home/.ssh/authorized_keys"
      done <"$admin_keys"
      chown "$user:$user" "$home/.ssh/authorized_keys"
      chmod 600 "$home/.ssh/authorized_keys"
      if [ -s "$home/.ssh/authorized_keys" ]; then
        ssh_ready=1
        say "   ssh key copy: '$SUDO_USER' keys copied — you can now:  ssh $user@<this-host>"
        say "   (key-only, password stays locked. The copy is a point-in-time snapshot: revoking a key"
        say "    on '$SUDO_USER' later does NOT revoke it here — remove it from $home/.ssh/authorized_keys too."
        say "    Didn't want the copy? BUZAI_COPY_SSH_KEYS=0 on a re-run, or edit that file.)"
      else
        say "   ssh key copy: no usable keys found in $admin_keys — switch in with: sudo -u $user -i"
      fi
    else
      say "   ssh key copy: '$SUDO_USER' has no authorized_keys (password-auth ssh?) — switch in with: sudo -u $user -i"
    fi
  fi

  # verify (a): is the per-user systemd MANAGER running? (tests linger, not login env)
  # The manager can take a moment to come up right after enable-linger.
  local state="" i
  for i in 1 2 3 4 5; do
    state="$(su -s /bin/bash "$user" -c "XDG_RUNTIME_DIR=/run/user/$uid systemctl --user is-system-running 2>/dev/null" || true)"
    case "$state" in running|degraded) break ;; esac
    sleep 1
  done
  case "$state" in
    running)  say "   manager probe: running" ;;
    degraded) say "   manager probe: degraded (a user unit failed — inspect later with systemctl --user status)" ;;
    *) fail "per-user systemd manager not up for '$user' (got: '${state:-none}') — linger or /run/user/$uid problem; see docs/HOST-BOOTSTRAP.md" ;;
  esac

  # verify (b): is a fresh LOGIN correctly wired? (proves 1c persisted AND is sourced)
  # Accept running|degraded exactly like probe (a) — is-system-running exits nonzero on
  # degraded, and a failed user unit must not be misdiagnosed as broken .profile wiring.
  if su - "$user" -c 'test -n "$XDG_RUNTIME_DIR" && case "$(systemctl --user is-system-running 2>/dev/null)" in running|degraded) exit 0 ;; *) exit 1 ;; esac'; then
    say "   login-wiring probe: ok"
  else
    fail "fresh login for '$user' can't reach the user bus — login-file wiring (step 1c, ${login_file##*/}) didn't take; see docs/HOST-BOOTSTRAP.md"
  fi

  say ""
  say "Host bootstrap complete. Next, log in as the service account and run the user phase:"
  say ""
  if [ -n "$ssh_ready" ]; then
    say "    ssh $user@<this-host>     # from your workstation — your key was copied above"
  else
    say "    sudo -u $user -i          # no ssh key on the account — switch in from here"
  fi
  say "    curl -fsSL ${BUZAI_REPO}/raw/main/install.sh | bash"
  say ""
  say "(or: git clone ${BUZAI_REPO}.git ~/buzai && cd ~/buzai && make setup)"
}

# ------------------------------------------------------------------ user phase
user_phase() {
  say "== buzai install (user phase) — account: $(id -un), workspace: $BUZAI_WORKDIR =="

  # Running the bootstrap one-liner WITHOUT sudo doesn't error — uid selection
  # would land here and install the whole stack into your personal admin account.
  refuse_admin_account

  # Already inside a checkout? (running ./install.sh from the repo root)
  if [ -f Makefile ] && [ -d trust ] && [ -d scripts ]; then
    say "   running from a repo checkout — skipping fetch"
    exec make setup
  fi

  # An existing EMPTY dir is fetchable (a failed earlier fetch must not deadlock re-runs);
  # an existing non-checkout, non-empty dir is refused.
  if [ -d "$BUZAI_WORKDIR" ] && [ -f "$BUZAI_WORKDIR/Makefile" ] && [ -d "$BUZAI_WORKDIR/trust" ]; then
    say "   workspace exists — continuing setup there"
  elif [ -d "$BUZAI_WORKDIR" ] && [ -n "$(ls -A "$BUZAI_WORKDIR" 2>/dev/null)" ]; then
    fail "$BUZAI_WORKDIR exists and isn't a buzai checkout — refusing to touch it (set BUZAI_WORKDIR to use another path)"
  else
    if command -v git >/dev/null 2>&1; then
      say "   cloning $BUZAI_REPO"
      git clone --depth 1 "$BUZAI_REPO.git" "$BUZAI_WORKDIR" 2>/dev/null \
        || git clone --depth 1 "$BUZAI_REPO" "$BUZAI_WORKDIR"
    else
      say "   git not found — fetching tarball"
      mkdir -p "$BUZAI_WORKDIR"
      curl -fsSL "$BUZAI_REPO/archive/refs/heads/main.tar.gz" \
        | tar -xz --strip-components=1 -C "$BUZAI_WORKDIR"
    fi
  fi

  cd "$BUZAI_WORKDIR"
  exec make setup
}

# ----------------------------------------------------------------- setup phase
# The step runner behind `make setup`. Shell-first by design: each step is an
# inline postcondition probe + action. A satisfied probe skips the step; a step
# needing a human prints the exact command and exits 0 — re-running `make setup`
# resumes past it once its postcondition (or marker) holds. Re-runs are the
# resumption mechanic, not extra "manual steps".
#
# Probe table (docs/SETUP.md is the reference the pauses point into):
#   claude-install   -> command -v claude
#   auth (PAUSE)     -> ~/.claude/.credentials.json non-empty
#   connectors (PAUSE, optional) -> marker .buzai/setup-connectors-done (no local artifact exists)
#   venv             -> .venv/bin/python executable
#   test             -> run every time (cheap)
#   trust-install    -> hooks present in .claude/settings.json
#   prime-consent (PAUSE) -> marker .buzai/setup-consent-done (consent has no CLI flag or local artifact)
#   service-install  -> unit file present
#   service-start    -> systemctl --user is-active
#   liveness         -> the success criterion: PID-scoped relay socket, never just "is-active"

step()   { printf '\n\033[36m== %s ==\033[0m\n' "$*"; }
pause_exit() { printf '\n%s\n\nre-run  make setup  when done — it resumes where you left off.\n' "$*"; exit 0; }

# Marker-confirmed step: no probeable local artifact exists, so completion is
# recorded in a gitignored marker. At a TTY we ask; piped, we print and exit.
confirm_or_pause() {
  local marker=".buzai/$1"; shift
  [ -f "$marker" ] && return 0
  printf '%s\n' "$*"
  if [ -t 0 ]; then
    local a=""
    read -r -p "Done (or not needed)? [y/N] " a
    case "$a" in y|Y) touch "$marker"; return 0 ;; esac
  fi
  pause_exit "(when finished:  touch $marker  — or answer y here on the next run)"
}

setup_phase() {
  [ -f Makefile ] && [ -d trust ] && [ -d scripts ] || fail "run from the repo root (e.g. cd ~/buzai)"
  # Same footgun as the user phase, reachable directly via `git clone` + `make setup`
  # in a personal admin account — guard this entry point too.
  refuse_admin_account
  export PATH="$HOME/.local/bin:$PATH"
  mkdir -p .buzai

  say "buzai setup — probes each step, skips what's already done, pauses only where a human is required."

  # Known trap, checked before anything else: an inference-only token silently
  # breaks Remote Control registration AND overrides the interactive login.
  if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    fail "CLAUDE_CODE_OAUTH_TOKEN is set. That token type cannot register Remote Control (the service would start 'active' but never appear at claude.ai/code) and it overrides 'claude auth login'. Unset it — in this shell and any service environment — then re-run make setup."
  fi

  step "1/9 Claude Code binary"
  if command -v claude >/dev/null 2>&1; then
    say "   ok — $(claude --version 2>/dev/null | head -1)"
  else
    say "   installing (official installer, per-user, no root)"
    curl -fsSL https://claude.ai/install.sh | bash
    export PATH="$HOME/.local/bin:$PATH"
    command -v claude >/dev/null 2>&1 \
      || fail "claude still not on PATH — run 'source ~/.profile' (or re-login) and re-run make setup"
    say "   ok — installed"
  fi

  step "2/9 Claude.ai subscription auth (manual pause #1)"
  if [ -s "$HOME/.claude/.credentials.json" ]; then
    say "   ok — credentials present"
  else
    pause_exit "Log in once, interactively:

    make auth

Pick \"1. Claude account with subscription\" (NOT the Console/API option), open the
printed OAuth URL in any browser, paste the code back. Details + first-run prompts
(folder trust, fullscreen renderer): docs/SETUP.md §3."
  fi

  step "3/9 Managed connectors (manual pause #2 — optional)"
  confirm_or_pause setup-connectors-done \
"Enable the connectors you want at  https://claude.ai/settings/connectors  (browser,
any machine), then classify them for the trust gate: docs/SETUP.md §4 and
'make trust-check'. Skip freely — you can add connectors any time."

  step "4/9 Pinned Python venv"
  if [ -x .venv/bin/python ]; then
    say "   ok — $(.venv/bin/python --version)"
  else
    make venv
  fi

  step "5/9 Test suite"
  make test

  step "6/9 Trust gate wiring"
  if grep -qs 'trust/bin/gate' .claude/settings.json; then
    say "   ok — hooks present in .claude/settings.json"
  else
    make trust-install
    say "   note: hooks load at session start — if you had an interactive claude session open here, restart it."
  fi

  step "7/9 Remote Control consent prime (manual pause #3)"
  confirm_or_pause setup-consent-done \
"One-time interactive consent — a headless service cannot answer these prompts, and
without them the service runs but silently never registers (the #1 trap):

    make prime-consent
      \"Enable Remote Control? (y/n)\"      -> y
      \"Spawn mode for this project [1/2]\" -> 1   (same-dir)

Confirm the session appears at claude.ai/code (same account), then exit with
Ctrl-D Ctrl-D. Details: docs/SETUP.md §6."

  step "8/9 Always-on service"
  if [ -f "$HOME/.config/systemd/user/claude-remote.service" ]; then
    say "   ok — unit installed"
  else
    make service-install
  fi
  if systemctl --user is-active --quiet claude-remote; then
    say "   ok — service active"
  else
    make service-start
    sleep 3
  fi

  step "9/9 Liveness (the real success check — not just 'active')"
  if make liveness; then
    say ""
    say "Setup complete. First use: attach from claude.ai/code or the Claude mobile app's"
    say "Code tab, signed into the SAME account — the session appears by account discovery."
    say "Then verify end to end:  make smoke  (and docs/SMOKE-TEST.md)."
  else
    fail "service is up but NOT-LIVE (no relay socket). Most common cause: the consent
prime (step 7) wasn't actually completed — re-run 'make prime-consent', confirm the
session at claude.ai/code, then 'make service-restart' and 'make liveness'.
More: docs/TROUBLESHOOTING.md"
  fi
}

# ---------------------------------------------------------------------- entry
case "${1:-}" in
  --setup)
    setup_phase
    ;;
  *)
    if [ "$(id -u)" -eq 0 ]; then
      root_phase "$BUZAI_USER"
    else
      user_phase
    fi
    ;;
esac
