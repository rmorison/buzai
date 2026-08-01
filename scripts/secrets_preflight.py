"""Fail closed if secrets are miswired. Used as the systemd `ExecStartPre` and as
the smoke test's SC0 gate: the service / smoke test must not proceed if a secret
file has loose perms or a secrets file is tracked by git.

Checks: every secrets env-file and `~/.claude/.credentials.json` must be mode 0600
(no group/other bits); no real secrets file may be tracked by git. Core checks
(`perm_problems`, `tracked_problems`) are pure for testing.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

SECRETS_DIR = Path.home() / ".config" / "buzai" / "secrets"
CREDS = Path.home() / ".claude" / ".credentials.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ENV_DIR = REPO_ROOT / "deploy" / "env"


def perm_problems(paths) -> list[str]:
    probs = []
    for p in paths:
        p = Path(p)
        if not p.exists():
            continue
        mode = stat.S_IMODE(p.stat().st_mode)
        if mode & 0o177:  # any exec, group, or other bit (0600/0400 only)
            probs.append(f"{p} is {oct(mode)}, must be 0600")
    return probs


def tracked_problems(paths, is_tracked) -> list[str]:
    return [f"{p} is tracked by git (secrets must be untracked)" for p in paths if is_tracked(p)]


def _repo_secret_candidates() -> list[Path]:
    """In-repo files that would leak a real secret if committed: deploy/env/*.env,
    excluding the *.env.example templates. These — not the ~/.config secrets, which
    live outside the repo and can never be tracked — are what the git check must
    actually inspect to honor SC0's 'no secrets tracked by git'."""
    if not DEPLOY_ENV_DIR.exists():
        return []
    return [p for p in DEPLOY_ENV_DIR.glob("*.env") if not p.name.endswith(".env.example")]


def _git_tracked(path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--error-unmatch", str(path)],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def problems(secret_files, repo_secrets, creds, is_tracked) -> list[str]:
    """The safety verdict, as a pure function (testable): every secrets file and the
    credentials file must be 0600, and no real secrets file may be tracked by git.

    NOTE: zero local secret files is NOT a problem. A managed-connector-only adopter
    (all connectors via claude.ai, OAuth in credentials.json) legitimately has no
    local-MCP env-files. The preflight guards the *safety* of whatever secrets exist,
    not their existence. (`credentials.json` perms are still checked below.)
    """
    paths = list(secret_files) + list(repo_secrets)
    return perm_problems(paths + [creds]) + tracked_problems(paths, is_tracked)


def main(argv=None) -> int:
    secret_files = list(SECRETS_DIR.glob("*.env")) if SECRETS_DIR.exists() else []
    repo_secrets = _repo_secret_candidates()
    probs = problems(secret_files, repo_secrets, CREDS, _git_tracked)
    if not secret_files:
        # Informational, not fatal: managed-connector-only setups have no local secrets.
        print(
            f"secrets-preflight: no local secret files in {SECRETS_DIR} — fine for a "
            "managed-connector-only setup; credentials.json perms still checked.",
            file=sys.stderr,
        )
    if probs:
        for p in probs:
            print(f"secrets-preflight FAIL: {p}", file=sys.stderr)
        return 1
    print("secrets-preflight OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
