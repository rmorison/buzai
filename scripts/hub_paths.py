"""Resolve the one location where hub content lives, and refuse the unsafe answers.

Hubs are the principal's personal knowledge base, and they do **not** live in this
checkout. This repo is public and ships only scaffolds (`hubs/README.md`,
`hubs/_example-hub.md`); real hub content lives in a private git repository outside
the checkout, at `$BUZAI_HUBS_DIR`, defaulting to `~/hubs`.

This module is the single place that path is computed. Everything that touches hubs
imports `hub_dir()` instead of joining `~/hubs` for itself, so there is exactly one
definition of "where hubs live" and exactly one place the refusal below lives. The
returned `HubLocation` carries the path *and* where the answer came from, so every
consumer can print it and a misconfiguration shows up in the journal instead of
silently writing personal content to the wrong store.

The refusal is the point of the module. A hub path landing inside the checkout would
put personal content one `git add` away from a public remote, and `.gitignore` alone
is not a barrier. Three shapes are rejected:

  * the path is *inside* the checkout — the direct leak;
  * the path *contains* the checkout — a hub repo `git init`ed above this one would
    swallow it, so the public checkout becomes hub content;
  * run from a linked worktree, the path is inside or contains the *main* checkout
    that worktree belongs to. "The checkout" is the directory this file sits in, so
    from a worktree it is the worktree, and a hub elsewhere in the main checkout
    would pass both checks above. `main_checkout()` asks git for the main checkout,
    and only when `.git` is a file: a normal clone (every deployed service) runs no
    git at all. If the lookup fails in a worktree, the resolver refuses rather than
    carry on unchecked.

Both comparisons run **after** symlink resolution. A `~/hubs` symlink whose target is
inside the checkout is the exact failure a lexical string comparison waves through,
so `expand()` (lexical) and `refusal()` (the decision, over already-resolved paths)
are kept pure and separate from `resolve()`, which does the one filesystem touch that
following symlinks requires, and `hub_dir()`, which reads the environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_VAR = "BUZAI_HUBS_DIR"
DEFAULT_DIR_NAME = "hubs"
DEFAULT_SOURCE = f"default ~/{DEFAULT_DIR_NAME}"
GIT_TIMEOUT_SECONDS = 10


class HubPathError(Exception):
    """The configured hub location is unusable.

    Always fatal for the caller: continuing would write personal content somewhere it
    must never go, which is worse than not writing it at all.
    """


@dataclass(frozen=True)
class HubLocation:
    """Where hubs live (`path`) and which convention said so (`source`, for logging)."""

    path: Path
    source: str

    def __str__(self) -> str:
        return f"{self.path} (from {self.source})"


def is_configured(raw: str | None) -> bool:
    """True when the environment actually names a hub location. Pure.

    Unset and empty-string are deliberately the same thing: `Environment=BUZAI_HUBS_DIR=`
    in a unit file, an unexported shell variable, and a stray blank all arrive as `""`,
    and treating `""` as a path would silently mean the filesystem root.
    """
    return bool((raw or "").strip())


def source_of(raw: str | None) -> str:
    """Which convention answered — printed by consumers so a misconfiguration is visible."""
    return ENV_VAR if is_configured(raw) else DEFAULT_SOURCE


def expand(raw: str | None, home: Path) -> Path:
    """Raw env value -> an absolute, not-yet-symlink-resolved path. Pure and lexical.

    `~` expands against the injected `home` rather than the process environment, so the
    rule is testable. A relative value resolves against `home`, never the ambient cwd:
    sessions are spawned in whatever directory the caller happened to be in, and a
    cwd-relative store would move with them.
    """
    value = (raw or "").strip()
    if not value:
        return home / DEFAULT_DIR_NAME
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    if value.startswith("~"):
        raise HubPathError(
            f"{ENV_VAR}={value!r}: '~user' paths are not supported — use an absolute path"
        )
    path = Path(value)
    return path if path.is_absolute() else home / path


def refusal(resolved: Path, repo_root: Path, main_root: Path | None = None) -> str | None:
    """Why this hub path must not be used, or None if it is safe. Pure.

    All arguments must already be symlink-resolved — `resolve()` does that, and doing
    it here would make the decision untestable without a filesystem. `main_root` is the
    main checkout when `repo_root` is a linked worktree of it (see `main_checkout()`);
    left out, or equal to `repo_root`, the answer is exactly the one-checkout answer.
    """
    if resolved.is_relative_to(repo_root):
        return (
            f"{resolved} is inside the repo checkout ({repo_root}) — hub content must never "
            f"live in this public repo; point {ENV_VAR} at a path outside it"
        )
    if repo_root.is_relative_to(resolved):
        return (
            f"{resolved} contains the repo checkout ({repo_root}) — a hub repo there would "
            f"swallow this one; point {ENV_VAR} at a path that does not contain it"
        )
    if main_root is None or main_root == repo_root:
        return None
    if resolved.is_relative_to(main_root):
        return (
            f"{resolved} is inside the main checkout ({main_root}) that this worktree "
            f"belongs to — hub content must never live in this public repo; point "
            f"{ENV_VAR} at a path outside it"
        )
    if main_root.is_relative_to(resolved):
        return (
            f"{resolved} contains the main checkout ({main_root}) that this worktree "
            f"belongs to — a hub repo there would swallow it; point {ENV_VAR} at a path "
            f"that does not contain it"
        )
    return None


def _scrubbed_env() -> dict[str, str]:
    """The environment minus every `GIT_*` variable.

    An inherited `GIT_DIR`, `GIT_WORK_TREE` or `GIT_COMMON_DIR` would point the lookup
    at some other repository and hand back the wrong main checkout.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def main_checkout(repo_root: Path) -> Path | None:
    """The main checkout `repo_root` is a linked worktree of, or None. Raises `HubPathError`.

    None means there is no other checkout to protect: `repo_root/.git` is a directory
    (a normal clone — no git command runs), there is no `.git` at all, the main
    repository is bare, or the main checkout is `repo_root` itself.

    When `.git` is a file, `git worktree list --porcelain` answers: its first record is
    the main working tree, and carries a `bare` line when there is none. Asking git
    avoids hand-parsing `.git` and `commondir`. Any failure to get that answer is a
    refusal: from a worktree, an unchecked main checkout is the very leak this module
    exists to stop. That includes an answer that names no working tree, as git does
    for a main repository made with `--separate-git-dir`. The returned path is
    symlink-resolved.
    """
    marker = repo_root / ".git"
    if not marker.is_file():
        return None

    def fail(cause: str) -> HubPathError:
        return HubPathError(
            f"{repo_root} is a linked git worktree, but its main checkout could not be "
            f"found ({cause}) — refusing, since a hub path inside the main checkout "
            f"could not be ruled out"
        )

    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            env=_scrubbed_env(),
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        raise fail("git is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise fail(f"git did not answer within {GIT_TIMEOUT_SECONDS}s") from None
    except OSError as e:
        raise fail(f"git could not be run: {e}") from None
    if result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[0]
        raise fail(f"git worktree list exited {result.returncode}: {detail}")

    first_record = result.stdout.split("\n\n", 1)[0].splitlines()
    if not first_record or not first_record[0].startswith("worktree "):
        raise fail("git worktree list printed no worktree record")
    if "bare" in first_record[1:]:
        return None
    main = Path(first_record[0][len("worktree ") :]).resolve()
    if main == repo_root.resolve():
        return None
    if not (main / ".git").exists():
        # A main repository made with `--separate-git-dir` is listed by its git dir,
        # not its working tree, and keeps no pointer back to the tree. The same holds
        # for a main checkout that has since been deleted. Either way the checkout to
        # protect is unknown.
        raise fail(f"git names {main} as the main working tree, but it has no .git")
    return main


def resolve(raw: str | None, home: Path, repo_root: Path) -> HubLocation:
    """Expand, follow symlinks, and refuse the unsafe answers. Raises `HubPathError`.

    `Path.resolve()` is non-strict, so a hub directory that does not exist yet — every
    instance before it is initialized — resolves fine: the existing prefix is followed
    through symlinks and the remainder is appended. From a linked worktree, the main
    checkout it belongs to is refused too (`main_checkout()`).
    """
    root = repo_root.resolve()
    main_root = main_checkout(root)
    resolved = expand(raw, home).resolve()
    problem = refusal(resolved, root, main_root)
    if problem:
        raise HubPathError(problem)
    return HubLocation(resolved, source_of(raw))


def hub_dir() -> HubLocation:
    """The resolved hub location for this process — the entry point consumers import."""
    return resolve(os.environ.get(ENV_VAR), Path.home(), REPO_ROOT)


def main(argv=None) -> int:
    try:
        location = hub_dir()
    except HubPathError as e:
        print(f"hub-paths FAIL: {e}", file=sys.stderr)
        return 1
    print(f"hub-paths: hubs resolve to {location}")
    if not location.path.exists():
        print("  note: that directory does not exist yet — it has not been initialized")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
