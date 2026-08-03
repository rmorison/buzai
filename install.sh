#!/usr/bin/env bash
#
# buzai installer — one script, two phases, picked by who runs it.
#
#   ROOT phase (host bootstrap; run with sudo on a fresh host):
#       curl -fsSL https://raw.githubusercontent.com/rmorison/buzai/main/install.sh | sudo bash
#     Creates the dedicated unprivileged user (default: buzai), enables linger,
#     wires XDG_RUNTIME_DIR into its .bashrc, copies the invoking admin's
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

  # 1b. linger — lets `systemctl --user` services start at boot and survive logout
  if [ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null)" = "yes" ]; then
    say "   linger already enabled"
  else
    loginctl enable-linger "$user"
    say "   linger enabled"
  fi

  # 1c. persist XDG_RUNTIME_DIR — without it a fresh login can't reach the per-user
  #     systemd bus ("Failed to connect to bus", the #1 trip-up). Quoted heredoc keeps
  #     $(id -u) literal so it evaluates at each login.
  if grep -q 'XDG_RUNTIME_DIR=/run/user' "$home/.bashrc" 2>/dev/null; then
    say "   XDG_RUNTIME_DIR already wired in .bashrc"
  else
    tee -a "$home/.bashrc" >/dev/null <<'EOF'

# wire the per-user systemd bus for `systemctl --user`
export XDG_RUNTIME_DIR=/run/user/$(id -u)
EOF
    chown "$user:$user" "$home/.bashrc"
    say "   XDG_RUNTIME_DIR wired into .bashrc"
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
      install -d -m 700 -o "$user" -g "$user" "$home/.ssh"
      touch "$home/.ssh/authorized_keys"
      # append only keys not already present, so re-runs stay idempotent
      while IFS= read -r key; do
        case "$key" in ''|\#*) continue ;; esac
        grep -qxF "$key" "$home/.ssh/authorized_keys" \
          || printf '%s\n' "$key" >>"$home/.ssh/authorized_keys"
      done <"$admin_keys"
      chown "$user:$user" "$home/.ssh/authorized_keys"
      chmod 600 "$home/.ssh/authorized_keys"
      ssh_ready=1
      say "   ssh key copy: '$SUDO_USER' keys copied — you can now:  ssh $user@<this-host>"
      say "   (key-only, password stays locked; didn't want this? BUZAI_COPY_SSH_KEYS=0 on a re-run, or edit $home/.ssh/authorized_keys)"
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
  # degraded, and a failed user unit must not be misdiagnosed as broken .bashrc wiring.
  if su - "$user" -c 'test -n "$XDG_RUNTIME_DIR" && case "$(systemctl --user is-system-running 2>/dev/null)" in running|degraded) exit 0 ;; *) exit 1 ;; esac'; then
    say "   login-wiring probe: ok"
  else
    fail "fresh login for '$user' can't reach the user bus — .bashrc wiring (step 1c) didn't take; see docs/HOST-BOOTSTRAP.md"
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

  # Guard the #1 footgun: running the bootstrap one-liner WITHOUT sudo doesn't
  # error — uid selection would land here and install the whole stack into your
  # personal admin account, defeating the isolation the dedicated user exists for.
  if [ "${BUZAI_ALLOW_ADMIN_INSTALL:-0}" != "1" ] && id -nG | grep -qwE 'sudo|wheel|admin'; then
    fail "account '$(id -un)' is sudo-capable (member of sudo/wheel/admin) — refusing the user phase.
buzai belongs in its own unprivileged account. You probably meant one of:

    curl -fsSL ${BUZAI_REPO}/raw/main/install.sh | sudo bash    # bootstrap (note: sudo)
    ssh ${BUZAI_USER}@<host>   # then re-run this one-liner there (or: sudo -u ${BUZAI_USER} -i)

Really install into '$(id -un)'? Re-run with BUZAI_ALLOW_ADMIN_INSTALL=1."
  fi

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
