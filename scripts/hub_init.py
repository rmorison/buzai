"""Instantiate the private hub repository from this repo's tracked scaffolds.

`make hub-init` turns the public template into a working, versioned, personal hub
store: it creates the resolved hub directory (see `scripts/hub_paths.py` — the one
place that path is computed), `git init`s it, seeds the tracked scaffolds, writes a
README describing *this instance's* store, moves any real hub markdown that is still
sitting in the checkout out to the new repo, and makes the initial commit.

It deliberately **stops before the remote**. Creating that repository is the single
step where a wrong flag publishes the knowledge base, so it is never automated: the
exact owner-run commands are printed instead, in the style of `install.sh`'s manual
pauses. Re-running after the remote exists reports rather than repeats.

Idempotent and non-destructive, in the sense `scripts/install_hooks.py` uses: an
existing hub repo is reported and left completely alone — never re-seeded, never
clobbered — and a non-empty directory that is *not* a repo is refused rather than
initialized over. Anything that fails before the initial commit rolls back what this
script created, so a failed run leaves no half-built repo behind.

Migration is the one-time move of real hub content out of the public checkout.
"Real" is decided from git, not from a filename list: whatever `git ls-files` reports
under `hubs/` is a tracked scaffold and stays, everything else is personal content
and moves. It is **copied** into the hub repo, committed, and only then removed from
the checkout — so a failure anywhere in between costs nothing.

The `remote_expected` marker
----------------------------
`.buzai/remote-expected` inside the hub repo records when the store was created.
U6's durability check reads it: a hub repo older than the grace period that still has
no remote configured means nothing has ever gone off-box, which warns (never blocks
service start). It lives *inside* the repo so it is committed, and therefore survives
a restore-by-clone with the rest of the history.

Format: UTF-8 text. Blank lines and `#` comment lines are ignored; the first
remaining line is an ISO-8601 timestamp with an explicit UTC offset. Use
`render_marker()` / `parse_marker()` rather than re-deriving the format.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hub_paths import ENV_VAR, HubPathError, hub_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD_DIR = REPO_ROOT / "hubs"

GIT = "git"
DEFAULT_BRANCH = "main"
INSTANCE_README = "README.md"
MARKER = Path(".buzai") / "remote-expected"


class HubInitError(Exception):
    """Initialization cannot proceed, or could not be completed.

    Always fatal for the caller: the alternatives are writing personal content into a
    location that has not been established as safe, or reporting a store as ready when
    it is not.
    """


@dataclass(frozen=True)
class InitResult:
    """What `initialize` did, so `main` can report it and tests can assert on it."""

    status: str  # "created" | "exists"
    hub: Path
    seeded: list[str]
    migrated: list[str]
    commits: int
    remotes: list[str]


# --- git plumbing ---------------------------------------------------------------
#
# Every invocation targets the repo explicitly with `-C` (never an inherited cwd) and
# a nonzero returncode is a value, not an exception — the two rules the existing call
# sites in secrets_preflight.py and publish_gate.py already follow.


def git(args: list[str], git_bin: str = GIT) -> subprocess.CompletedProcess:
    """Run git, capturing output. Raises `HubInitError` only if git cannot be run."""
    try:
        return subprocess.run([git_bin, *args], capture_output=True, text=True)
    except OSError as e:
        raise HubInitError(
            f"cannot run {git_bin!r}: {e} — git must be installed and on PATH"
        ) from e


def git_ok(args: list[str], git_bin: str, what: str) -> str:
    """Run git and require success. Returns stdout; raises `HubInitError` on failure."""
    r = git(args, git_bin)
    if r.returncode != 0:
        detail = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}").splitlines()[-1]
        raise HubInitError(f"{what} failed: {detail}")
    return r.stdout


def commit_count(hub: Path, git_bin: str = GIT) -> int:
    """Commits on HEAD; 0 for a repo that has none (rev-list fails on an unborn HEAD)."""
    r = git(["-C", str(hub), "rev-list", "--count", "HEAD"], git_bin)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else 0


def remotes(hub: Path, git_bin: str = GIT) -> list[str]:
    """Configured remote names — empty means nothing has ever been pushed off-box."""
    r = git(["-C", str(hub), "remote"], git_bin)
    return r.stdout.split() if r.returncode == 0 else []


def tracked_scaffolds(source: Path, git_bin: str = GIT) -> set[str]:
    """Scaffold names under `source`, per git — the authority on scaffold vs. content.

    Paths come back relative to `source`. A failure here is fatal rather than an empty
    set: without git's answer, every personal file would look like a scaffold (and stay
    in the public checkout) or every scaffold would look like personal content.
    """
    out = git_ok(
        ["-C", str(source), "ls-files", "-z", "--", "."],
        git_bin,
        f"listing tracked scaffolds in {source}",
    )
    return {name for name in out.split("\0") if name}


# --- pure planning and rendering -------------------------------------------------


def plan_seed(tracked: Iterable[str]) -> list[str]:
    """Scaffolds to copy into the hub repo. Pure.

    Every tracked scaffold except `README.md`: the public one describes the *template*
    directory ("your content does not live here"), which is the opposite of what the
    hub store's own README needs to say. `render_readme` writes that one instead.
    """
    return sorted(name for name in tracked if name != INSTANCE_README)


def plan_migration(source: Path, tracked: Iterable[str]) -> list[str]:
    """Real hub files under `source` (recursively), as paths relative to it.

    Anything git does not track is personal content. Recursive because a hub that
    outgrew a single file becomes a folder, and the folder must move whole.
    """
    if not source.is_dir():
        return []
    known = set(tracked)
    found = []
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(source).as_posix()
        if rel not in known:
            found.append(rel)
    return sorted(found)


def render_marker(now: datetime) -> str:
    """The `remote_expected` marker file body. Pure. Format documented at module top."""
    return (
        "# buzai hub store: created at the ISO-8601 timestamp below.\n"
        "# The durability check warns when a store this old still has no remote\n"
        "# configured, which means nothing has ever been pushed off-box.\n"
        f"{now.isoformat(timespec='seconds')}\n"
    )


def parse_marker(text: str) -> datetime:
    """Creation timestamp out of a marker file body. Pure.

    Raises `ValueError` on anything unparseable — a garbled marker must surface as a
    problem, never be silently read as "no warning needed".
    """
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return datetime.fromisoformat(line)
    raise ValueError("marker file has no timestamp line")


def render_readme(hub: Path, now: datetime) -> str:
    """The hub store's own README — the private counterpart to `hubs/README.md`.

    Carries the restore procedure (R8) so the one thing needed to rebuild an instance
    travels inside the backup itself, not only in the public repo's docs.
    """
    return f"""# Hub store

The private, versioned knowledge base for this buzai instance: one canonical
markdown file — or, once it outgrows that, a folder — per life domain.

- Created: {now.isoformat(timespec="seconds")} by `make hub-init`.
- Path on this instance: `{hub}` (override with `{ENV_VAR}`, set in the service
  unit rather than a shell profile).

**This repository is private and stays private.** It holds personal content that
must never reach the public buzai repo; that repo tracks only scaffolds, and its
hub path resolver refuses any location inside the checkout.

## Restore onto a fresh instance

```bash
git clone <your-private-hub-remote> ~/hubs
```

That is the whole restore. Content, history, and review state come with the clone;
nothing in the public buzai checkout needs editing. If the store lives somewhere
other than `~/hubs`, clone it there and set `{ENV_VAR}` in the service unit.

## How this store works

- **One hub per domain.** Copy `_example-hub.md` to start a new one.
- **The hub wins** when a hub and an incoming brief disagree.
- **Every assistant change is one commit** with a message you can review in plain
  language, without the diff — you approve or reject per item, conversationally.
- **No secrets here.** Credentials live outside any repo, at `~/.config/buzai/`.

## Files

- `_example-hub.md` — scaffold to copy when starting a hub.
- `.buzai/remote-expected` — when this store was created; the durability check uses
  it to warn if the store still has no remote long afterwards.
"""


def commit_message(migrated: Iterable[str]) -> str:
    """Initial-commit message. Pure.

    Carries the instruction-source trailer from the start: the owner ran `make
    hub-init`, so this commit is owner-directed and history says so from commit one.
    """
    moved = list(migrated)
    body = "Seeded from the buzai public scaffolds by `make hub-init`."
    if moved:
        body += (
            f"\n\nMoved {len(moved)} existing hub file(s) out of the public checkout:\n"
            + "\n".join(f"  - {m}" for m in moved)
        )
    return f"Initialize hub store\n\n{body}\n\ninstruction-source: owner-directed\n"


def owner_instructions(hub: Path) -> str:
    """The manual pause: exactly what the owner runs to create and attach the remote.

    Never automated. `install.sh` pauses the same way for the steps a human must own,
    and this is the one command where a wrong flag publishes the knowledge base.
    """
    return f"""
NEXT STEP — create the private remote yourself. buzai will not do it for you:
this is the one command where a wrong flag publishes your knowledge base.

  1. Create an EMPTY repository, visibility PRIVATE. Do not add a README or a
     license — the hub repo already has its own initial commit.

       gh repo create hubs --private          # or use your host's web UI

  2. Confirm in the web UI that it really is private, then attach and push:

       git -C {hub} remote add origin <ssh-url-of-that-private-repo>
       git -C {hub} push -u origin {DEFAULT_BRANCH}

Use an SSH URL with a deploy key scoped to that one repository — never a token
embedded in the remote URL: git echoes the remote on failure, and the service
sends stderr to the journal.

re-run  make hub-init  when done — it reports the existing store rather than
re-seeding it."""


# --- side effects ----------------------------------------------------------------


def _rollback(hub: Path, existed: bool) -> None:
    """Undo what this run created, and only that.

    The create path is only entered when `hub` is absent or empty, so wiping its
    contents can never destroy pre-existing content. Migration copies are removed with
    it — the originals are still in the checkout, because they are deleted from there
    only after the commit succeeds.
    """
    try:
        if not existed:
            shutil.rmtree(hub, ignore_errors=True)
            return
        for child in hub.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
    except OSError:
        pass  # best effort: the original failure is what the caller must see


def _copy_into(source: Path, hub: Path, rel: str) -> None:
    """Copy one file into the hub repo, refusing to overwrite and never raising raw OSError."""
    dest = hub / rel
    if dest.exists():
        raise HubInitError(f"{dest} already exists — refusing to overwrite it with {source / rel}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, dest)
    except OSError as e:
        raise HubInitError(f"cannot copy {source / rel} to {dest}: {e}") from e


def _remove_migrated(source: Path, migrated: Iterable[str]) -> None:
    """Delete the checkout's copies once they are safely committed in the hub repo."""
    failed = []
    for rel in migrated:
        path = source / rel
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            failed.append(f"{path} ({e})")
    dirs = [p for p in source.rglob("*") if p.is_dir() and not p.is_symlink()]
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    if failed:
        raise HubInitError(
            "the hub repo was created and committed, but these files could not be removed "
            "from the public checkout — delete them by hand: " + ", ".join(failed)
        )


def initialize(
    hub: Path,
    source: Path,
    tracked: Iterable[str],
    now: datetime,
    git_bin: str = GIT,
) -> InitResult:
    """Create and seed the hub repo at `hub`, or report the one already there.

    `source` is the checkout's `hubs/` directory and `tracked` the scaffold names git
    reports in it; everything else under `source` is personal content and is migrated.
    """
    probe = git(["--version"], git_bin)
    if probe.returncode != 0:
        raise HubInitError(f"`{git_bin} --version` failed — git is not usable")

    if hub.exists() and not hub.is_dir():
        raise HubInitError(f"{hub} exists and is not a directory — refusing to initialize over it")
    if (hub / ".git").exists():
        return InitResult("exists", hub, [], [], commit_count(hub, git_bin), remotes(hub, git_bin))
    if hub.is_dir() and any(hub.iterdir()):
        raise HubInitError(
            f"{hub} already exists, is not empty, and is not a git repository — refusing to "
            f"initialize over it; move it aside, or point {ENV_VAR} at another location"
        )

    seed = plan_seed(tracked)
    migrate = plan_migration(source, tracked)
    existed = hub.is_dir()
    try:
        hub.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise HubInitError(f"cannot create {hub}: {e}") from e

    try:
        git_ok(["-C", str(hub), "init", "-q", "-b", DEFAULT_BRANCH], git_bin, "git init")
        # Checked here, inside the new repo, so the answer accounts for its config —
        # and early, so an unset identity fails before anything is moved.
        if git(["-C", str(hub), "var", "GIT_COMMITTER_IDENT"], git_bin).returncode != 0:
            raise HubInitError(
                "git has no commit identity, so the initial commit would fail — set one: "
                'git config --global user.name "Your Name" '
                '&& git config --global user.email "you@example.com"'
            )
        for name in seed:
            _copy_into(source, hub, name)
        (hub / INSTANCE_README).write_text(render_readme(hub, now))
        (hub / MARKER).parent.mkdir(parents=True, exist_ok=True)
        (hub / MARKER).write_text(render_marker(now))
        for rel in migrate:
            _copy_into(source, hub, rel)
        git_ok(["-C", str(hub), "add", "-A"], git_bin, "git add")
        git_ok(
            ["-C", str(hub), "commit", "-q", "-m", commit_message(migrate)], git_bin, "git commit"
        )
    except Exception:
        _rollback(hub, existed)
        raise

    _remove_migrated(source, migrate)
    return InitResult(
        "created", hub, seed, migrate, commit_count(hub, git_bin), remotes(hub, git_bin)
    )


def main(argv=None) -> int:
    try:
        location = hub_dir()
    except HubPathError as e:
        print(f"hub-init FAIL: {e}", file=sys.stderr)
        return 1
    print(f"hub-init: hubs resolve to {location}")

    try:
        tracked = tracked_scaffolds(SCAFFOLD_DIR)
        result = initialize(location.path, SCAFFOLD_DIR, tracked, datetime.now(UTC))
    except (HubInitError, OSError) as e:
        print(f"hub-init FAIL: {e}", file=sys.stderr)
        return 1

    if result.status == "exists":
        print(
            f"hub-init: {result.hub} is already a hub repo ({result.commits} commit(s)) — "
            "left untouched"
        )
    else:
        print(f"hub-init: created the hub repo at {result.hub} on branch {DEFAULT_BRANCH}")
        print(f"  seeded:   {', '.join(result.seeded) or 'nothing (no scaffolds tracked)'}")
        if result.migrated:
            print(
                f"  migrated: {len(result.migrated)} file(s) out of {SCAFFOLD_DIR} — "
                f"{', '.join(result.migrated)}"
            )
        print(f"  marker:   {MARKER} (creation timestamp; the durability check reads it)")
        print(f"  commit:   {result.commits} initial commit")

    if result.remotes:
        print(f"hub-init: remote(s) configured: {', '.join(result.remotes)}")
    else:
        print(owner_instructions(result.hub))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
