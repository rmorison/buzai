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


def _points_back(tree: Path, common_dir: Path) -> bool:
    """True when `tree/.git` leads to `common_dir`: the tree really is that repo's.

    `.git` is either the git dir itself or a file reading `gitdir: <path>`, relative to
    the tree when not absolute. Anything else (missing, unreadable) is False.
    """
    marker = tree / ".git"
    try:
        if marker.is_dir():
            return marker.resolve() == common_dir
        if marker.is_file():
            first = marker.read_text().splitlines()[0]
            if first.startswith("gitdir: "):
                return (tree / first[len("gitdir: ") :].strip()).resolve() == common_dir
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return False


def main_checkout(repo_root: Path) -> Path | None:
    """The main checkout `repo_root` is a linked worktree of, or None. Raises `HubPathError`.

    None means there is no other checkout to protect: `repo_root/.git` is a directory
    (a normal clone — no git command runs), there is no `.git` at all, the main
    repository is bare, or the main checkout is `repo_root` itself.

    When `.git` is a file, git answers. `git worktree list --porcelain` names the main
    working tree in its first record, or marks it `bare` when there is none. But git
    2.34 builds that name from the shared git dir, so it is only trusted when checked:

      * if the shared git dir sets `core.worktree` (submodules do, and so does a repo
        whose tree was moved away from its git dir), in `config` or, under
        `extensions.worktreeConfig`, in `config.worktree`, that is the main working
        tree;
      * otherwise the named tree counts only if its `.git` leads back to the shared
        git dir. A repo made with `--separate-git-dir` fails this, since git names the
        git dir, which keeps no pointer to the tree.

    Any failure to get a checked answer is a refusal: from a worktree, an unchecked
    main checkout is the very leak this module exists to stop. The returned path is
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

    def git(*args: str, ok: tuple[int, ...] = (0,)) -> tuple[int, str]:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args],
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
        if result.returncode not in ok:
            detail = (result.stderr.strip().splitlines() or ["no output"])[0]
            raise fail(f"git {args[0]} {args[1]} exited {result.returncode}: {detail}")
        return result.returncode, result.stdout

    _, listing = git("worktree", "list", "--porcelain")
    first_record = listing.split("\n\n", 1)[0].splitlines()
    if not first_record or not first_record[0].startswith("worktree "):
        raise fail("git worktree list printed no worktree record")
    if "bare" in first_record[1:]:
        return None

    _, common = git("rev-parse", "--git-common-dir")
    common_dir = (repo_root / common.strip()).resolve()

    def config_value(name: str, key: str, *flags: str) -> str | None:
        rc, out = git("config", "--file", str(common_dir / name), *flags, "--get", key, ok=(0, 1))
        return out.strip() if rc == 0 and out.strip() else None

    # With extensions.worktreeConfig, git reads config.worktree after config, so a
    # core.worktree there wins (git-worktree(1) tells users to move it there).
    configured = None
    if config_value("config", "extensions.worktreeConfig", "--type=bool") == "true":
        if (common_dir / "config.worktree").is_file():
            configured = config_value("config.worktree", "core.worktree")
    configured = configured or config_value("config", "core.worktree")
    if configured and configured.startswith("~"):
        raise fail(f"core.worktree is {configured!r}, which can't be resolved unambiguously")
    if configured:
        main = (common_dir / configured).resolve()
    else:
        main = Path(first_record[0][len("worktree ") :]).resolve()
        if not _points_back(main, common_dir):
            raise fail(
                f"git names {main} as the main working tree, but its .git does not "
                f"lead back to the repository's git dir {common_dir}"
            )
    return None if main == repo_root.resolve() else main


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
