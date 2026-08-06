"""The deployment's structural preflight: refuse to start on a layout that can leak.

Wired as the systemd `ExecStartPre` and as the smoke test's SC0 gate. Pre-commit hooks
are a `make dev` developer target and are verified absent from deployments, so this is
the *one* control that actually runs there — which makes it the place the layout
guarantees (R3, R15/R16/R17) become enforceable rather than conventional.

The fatal / warning split — the whole point of this module
----------------------------------------------------------
`ExecStartPre` failure blocks service start, and the unit sets `StartLimitBurst=5`:
five failed starts in five minutes leave the unit `failed` until a human logs in. So a
check that fails here does not degrade the assistant, it *ends* it. Which conditions
earn that is therefore a deliberate decision, not a matter of severity feel:

**FATAL** — leak conditions. Personal state is somewhere it can reach the public repo,
or the instance has a way to push to it. Refusing to run is strictly better than
running:

  * `hubs/` in the checkout holds anything beyond the named scaffolds (R16) — whether
    or not the public repo already tracks it, which makes it worse, not exempt;
  * a real `.env` sits under `deploy/env/` (R17);
  * the resolved hub path is inside — or contains — the checkout (R3);
  * the hub push credential is not 0600, or is tracked by *any* git repo (R15);
  * a credential helper is configured for the **public** origin (R4/R3).

**WARNING** — durability conditions. Knowledge is being recorded but is not (yet)
safely off-box. Nothing is leaking, and the assistant still works:

  * a hub repo older than `REMOTE_GRACE_SECONDS` with no remote configured;
  * an unpushed backlog (count and age of the oldest commit);
  * a dirty hub working tree;
  * a git repository *above* the hub directory — a dotfiles repo at `~` makes `~/hubs`
    a nested repo whose content the outer repo could swallow. That outer repo is the
    owner's own and of unknown visibility, so it is not proof of a leak the way the
    public checkout is; and failing here would take down every instance whose home
    directory is version-controlled.

Making any of those fatal converts degraded knowledge into a total assistant outage,
which is the trade this design explicitly refuses.

Not a no-op, by construction
----------------------------
The original tracked-check in this file was fed only paths *outside* the repo, so it
could never fire — a dead check shipping with full confidence
(`docs/solutions/architecture-patterns/running-claude-code-always-on.md`). Every check
below is therefore fed paths that can actually fail, and every one has a test that
plants the violation and proves it fires. `_git_tracked` in particular now runs from
the file's **own** directory so git discovers whatever repo encloses it, which is the
only way "tracked by any repo" can be true of `~/.ssh/buzai-hub`.

Legitimate empty states are not violations
------------------------------------------
Per that same learning (§6(f)): a fresh instance with no hub repo, a scaffold-only
`hubs/`, and a managed-connector-only box with zero local secret files all pass
cleanly. Absence is reported as a note, never as a failure.

Shape: pure core, injected side effects. `perm_problems` / `tracked_problems` /
`fatal_problems` / `warnings_for` decide; `_git_tracked`, `hub_checkout_problems` and
`inspect_hub` gather. Nothing new is reimplemented here — the hub path rule comes from
`hub_paths`, scaffold-vs-content from `hub_init`, the backlog from `hub_commit`, and
the public-origin credential-helper check from `hub_remote`.

`assess()` is the wiring: it composes those checks with the module constants above and
is what systemd actually runs. Well-tested checks wired up wrongly — one composed with
the wrong constant, one dropped from the list — is the same dead no-op wearing a
different hat, and it would leave the suite green. So every constant `assess()` reads
is a keyword argument defaulting to that constant, and the tests plant real violations
under temporary directories and drive the *real* composition through `main()`.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hub_commit import (  # noqa: E402
    GIT_TIMEOUT_SECONDS,
    Backlog,
    HubCommitError,
    default_backlog_path,
    dirty_paths,
    read_backlog,
)
from scripts.hub_init import (  # noqa: E402
    MARKER,
    SCAFFOLDS,
    HubInitError,
    commit_count,
    parse_marker,
    plan_migration,
    remotes,
    tracked_paths,
)
from scripts.hub_paths import ENV_VAR, HubPathError, expand, refusal, source_of  # noqa: E402
from scripts.hub_remote import (  # noqa: E402
    LOCAL_ENV,
    GitRunner,
    default_git_runner,
    origin_credential_violations,
)

SECRETS_DIR = Path.home() / ".config" / "buzai" / "secrets"
CREDS = Path.home() / ".claude" / ".credentials.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ENV_DIR = REPO_ROOT / "deploy" / "env"
HUB_SCAFFOLD_DIR = REPO_ROOT / "hubs"

# The hub push credential, as `deploy/README.md` documents creating it: one ssh deploy
# key scoped to the hub repo. Not configurable — a second supported location would be a
# second thing to check and a second thing to get wrong.
HUB_KEY = Path.home() / ".ssh" / "buzai-hub"

# How long a hub store may exist with no remote before the durability warning fires.
# Long enough that `make hub-init` -> "create the repo yourself" -> attach can span a
# normal week; short enough that "I'll do it later" surfaces while it is still true.
REMOTE_GRACE_SECONDS = 7 * 24 * 3600.0

DAY = 86400.0


@dataclass(frozen=True)
class HubTarget:
    """Where hubs resolve to, and why that answer might be fatal.

    `problem` is filled from `hub_paths.refusal()` — the non-raising half of the
    resolver — precisely so this module can *classify* a bad hub path as one fatal
    finding among others instead of aborting before the remaining checks have run.
    """

    path: Path | None
    description: str
    problem: str | None = None


@dataclass(frozen=True)
class HubState:
    """The hub store as it is right now. Gathered by `inspect_hub`, judged by `warnings_for`."""

    exists: bool = False
    is_repo: bool = False
    created: datetime | None = None
    marker_error: str = ""
    remotes: tuple[str, ...] = ()
    commits: int = 0
    backlog: Backlog = field(default_factory=Backlog)
    dirty: tuple[str, ...] = ()
    inspect_error: str = ""
    enclosing_repo: str | None = None


@dataclass(frozen=True)
class Findings:
    """The verdict. Only `fatal` blocks service start."""

    fatal: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def blocks_start(self) -> bool:
        return bool(self.fatal)


# --- pure: the pre-existing secrets checks (unchanged contract) --------------------


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


def problems(secret_files, repo_secrets, creds, is_tracked) -> list[str]:
    """The secrets safety verdict, as a pure function (testable): every secrets file and
    the credentials file must be 0600, and no real secrets file may be tracked by git.

    NOTE: zero local secret files is NOT a problem. A managed-connector-only adopter
    (all connectors via claude.ai, OAuth in credentials.json) legitimately has no
    local-MCP env-files. The preflight guards the *safety* of whatever secrets exist,
    not their existence. (`credentials.json` perms are still checked below.)
    """
    paths = list(secret_files) + list(repo_secrets)
    return perm_problems(paths + [creds]) + tracked_problems(paths, is_tracked)


# --- pure: the structural checks ---------------------------------------------------


def env_file_problems(paths: Iterable[Path]) -> list[str]:
    """Real env-files inside the checkout. Pure. R16/R17.

    Fatal on *existence*, not on perms or tracking: `deploy/env/*.env` is gitignored, so
    a real value there is one `git add -f`, one rewritten ignore rule, or one archive of
    the working tree away from being published. The repo tracks `.example` scaffolds and
    nothing else; real values belong outside it.
    """
    return [
        f"{p} holds real values inside the checkout — the repo may track only "
        f"*.env.example scaffolds; move it to {SECRETS_DIR}/ (0600) and delete this copy"
        for p in paths
    ]


def hub_content_problems(content: Iterable[str], source: Path, tracked: Iterable[str]) -> list[str]:
    """Personal hub content sitting in the public checkout. Pure. R3/R16.

    `content` is whatever `hub_init.plan_migration` found under `source` that is not a
    named scaffold; `tracked` is the subset the public repo already has in its index.

    Tracking does not excuse a file — it aggravates it, and gets its own remedy. The
    earlier version of this check treated "tracked under `hubs/`" as *proof* of being a
    scaffold, so a personal hub file that had been `git add`ed was the one case exempt
    from the check whose entire job is keeping hub content out of the public repo.
    """
    staged = set(tracked)
    return [
        (
            f"{source / rel} is personal hub content that the PUBLIC repo already tracks — "
            f"it is one push from being published; take it out of the index with "
            f"`git rm --cached {source / rel}` and move the content into the private hub "
            f"store (`make hub-init` does both)"
            if rel in staged
            else f"{source / rel} is personal hub content inside the public checkout — hub "
            f"content lives in the private hub repo; run `make hub-init` to move it out"
        )
        for rel in content
    ]


def resolve_hub_target(raw: str | None, home: Path, repo_root: Path) -> HubTarget:
    """Where hubs resolve to and whether that is fatal. Never raises.

    The resolver's own entry point raises, which would abort the preflight before the
    secrets and credential checks ran. Here a bad hub path is one finding among several,
    and the resolved path is printed either way so a mismatch is visible in the journal.
    """
    try:
        path = expand(raw, home).resolve()
    except HubPathError as e:
        return HubTarget(None, f"unresolvable ({e})", str(e))
    return HubTarget(path, f"{path} (from {source_of(raw)})", refusal(path, repo_root.resolve()))


def fatal_problems(
    *,
    secret_files: Sequence[Path],
    repo_secrets: Sequence[Path],
    creds: Path,
    hub_credentials: Sequence[Path],
    hub_content: Sequence[str],
    hub_path_problem: str | None,
    origin_violations: Sequence[str],
    is_tracked: Callable[[Path], bool],
) -> list[str]:
    """Every leak condition, in one pure function. Blocks service start.

    `hub_credentials` goes through the same perms + tracked pair as the secrets files:
    a deploy key that can write the private hub repo is a secret like any other, and a
    dotfiles repo that tracks it publishes push access to the knowledge base.
    """
    return [
        *problems(secret_files, repo_secrets, creds, is_tracked),
        *env_file_problems(repo_secrets),
        *hub_content,
        *perm_problems(hub_credentials),
        *tracked_problems([p for p in hub_credentials if Path(p).exists()], is_tracked),
        *([hub_path_problem] if hub_path_problem else []),
        *origin_violations,
    ]


# --- pure: the durability warnings -------------------------------------------------


def _age(seconds: float) -> str:
    return f"{seconds / DAY:.1f} day(s)" if seconds >= DAY else f"{seconds / 3600:.1f} hour(s)"


def stale_remote_warning(
    hub: Path, state: HubState, now: datetime, grace_seconds: float = REMOTE_GRACE_SECONDS
) -> str | None:
    """A hub store that has held commits for a while and still has no remote. Pure.

    Not fatal, deliberately: the owner creates the private remote by hand (a wrong flag
    there publishes the knowledge base), so the window between `make hub-init` and the
    remote existing is an expected part of setup, not a misconfiguration.
    """
    if state.remotes or state.commits == 0:
        return None
    if state.created is not None and (now - state.created).total_seconds() < grace_seconds:
        return None
    when = (
        f"created {state.created.isoformat(timespec='seconds')}, "
        f"{_age((now - state.created).total_seconds())} ago"
        if state.created is not None
        else "creation time unknown"
    )
    return (
        f"{hub} has {state.commits} commit(s) and NO remote configured — nothing has ever "
        f"been pushed off-box, so a lost disk loses the knowledge base ({when}); "
        f"run `make hub-init` for the owner-run commands that attach a private remote"
    )


def backlog_warning(state: HubState, now: datetime) -> str | None:
    """Commits that exist locally but never reached the remote. Pure.

    Reported at *any* size: fine-grained tokens expire on a ~30-day default, so a silent
    push failure is an expected steady state rather than an edge case, and service start
    is the one moment the operator reliably reads the journal.
    """
    if state.backlog.count == 0:
        return None
    age = state.backlog.oldest_age_seconds(now)
    oldest = f", oldest {_age(age)} old" if age is not None else ""
    reason = f" — last error: {state.backlog.last_error}" if state.backlog.last_error else ""
    return (
        f"{state.backlog.count} hub commit(s) have not reached the remote{oldest}{reason}; "
        f"`make hub-push` retries (an expired deploy-key/token is the usual cause)"
    )


def dirty_warning(hub: Path, state: HubState) -> str | None:
    """Hub content on disk that no commit describes. Pure.

    Invisible to the review loop until something commits it, which is a knowledge-
    integrity problem and not a leak — the next hub write reconciles it as unattributed.
    """
    if not state.dirty:
        return None
    return (
        f"{hub} has {len(state.dirty)} uncommitted path(s) ({', '.join(state.dirty[:5])}"
        f"{'…' if len(state.dirty) > 5 else ''}) — unversioned and invisible to review "
        f"until the next hub write reconciles them"
    )


def nested_repo_warning(hub: Path, state: HubState) -> str | None:
    """A git repo above the hub directory. Pure.

    A dotfiles repo at `~` makes `~/hubs` a nested repo: its content can be swallowed by
    the outer repo and pushed wherever *that* points. It warns rather than blocks because
    the outer repo is the owner's own and of unknown visibility — unlike the public
    checkout, which `resolve_hub_target` refuses outright — and because a version-
    controlled home directory is common enough that failing would strand real instances.
    """
    if not state.enclosing_repo:
        return None
    return (
        f"{hub} is nested inside the git repository at {state.enclosing_repo} — that outer "
        f"repo can commit and push hub content wherever it points; move the hub store "
        f"outside it, or add it to that repo's .gitignore and confirm it is private"
    )


def warnings_for(
    hub: Path | None,
    state: HubState,
    now: datetime,
    grace_seconds: float = REMOTE_GRACE_SECONDS,
) -> list[str]:
    """Every durability condition. Pure. NEVER blocks service start — see the module top."""
    if hub is None or not state.is_repo:
        return []
    warns = [
        w
        for w in (
            stale_remote_warning(hub, state, now, grace_seconds),
            backlog_warning(state, now),
            dirty_warning(hub, state),
            nested_repo_warning(hub, state),
        )
        if w
    ]
    if state.inspect_error:
        warns.append(f"cannot read the hub working tree state: {state.inspect_error}")
    if state.marker_error:
        warns.append(f"{state.marker_error} — the no-remote grace period cannot be judged")
    return warns


def notes_for(hub: Path | None, state: HubState) -> list[str]:
    """Legitimate empty states, reported so the journal shows what was actually checked."""
    if hub is None:
        return []
    if not state.exists:
        return [f"{hub} does not exist yet — no hub store on this instance (run `make hub-init`)"]
    if not state.is_repo:
        return [f"{hub} exists but is not a git repository yet (run `make hub-init`)"]
    return []


# --- side effects: gathering --------------------------------------------------------


def _repo_secret_candidates(env_dir: Path = DEPLOY_ENV_DIR) -> list[Path]:
    """In-repo files that would leak a real secret: deploy/env/*.env, minus the
    *.env.example templates. These — not the ~/.config secrets, which live outside the
    repo and can never be tracked — are what the in-repo checks must actually inspect."""
    if not env_dir.is_dir():
        return []
    return sorted(p for p in env_dir.glob("*.env") if not p.name.endswith(".env.example"))


def _git_tracked(path) -> bool:
    """Is `path` tracked by whatever git repo encloses it — not merely by this checkout?

    Run from the file's **own** directory so git discovers the enclosing repository by
    walking up. `git -C REPO_ROOT` could never see `~/.ssh/buzai-hub` in a dotfiles repo
    at `~`, and a check that cannot see its target is the dead no-op this file already
    shipped once. A nonzero returncode (no repo, path outside it, untracked) is a value,
    not an exception.
    """
    p = Path(path)
    parent = p.parent if p.parent.is_dir() else REPO_ROOT
    r = subprocess.run(
        ["git", "-C", str(parent), "ls-files", "--error-unmatch", "--", str(p)],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def hub_checkout_problems(source: Path = HUB_SCAFFOLD_DIR) -> list[str]:
    """Personal hub content found in the checkout's `hubs/`. R3/R16.

    What is content and what is a scaffold comes from `hub_init.SCAFFOLDS` — an
    explicit set, so git cannot be asked the wrong question. Git is still asked a
    narrower one (what does the public repo already track?), and only to choose the
    remedy; an *unanswerable* answer downgrades nothing, it adds a finding, because
    "cannot tell" reading as "clean" is the failure mode this file exists to avoid.
    A missing `hubs/` is clean: there is nothing there to leak.
    """
    if not source.is_dir():
        return []
    content = plan_migration(source, SCAFFOLDS)
    try:
        tracked = tracked_paths(source)
    except HubInitError as e:
        # Every content file below is still a finding; what cannot be answered is only
        # which of them the index also holds, so the extra remedy is named here rather
        # than asserted per-file. The unanswerable question is itself reported: "cannot
        # tell" must never read as "clean".
        return [
            f"cannot determine which files under {source} the public repo tracks — treated "
            f"as unverified rather than clean: {e}; check each path below with `git rm "
            f"--cached` as well as moving it",
            *hub_content_problems(content, source, ()),
        ]
    return hub_content_problems(content, source, tracked)


def read_marker(hub: Path) -> tuple[datetime | None, str]:
    """(creation timestamp, error). A missing marker is neither — a pre-U2 or
    hand-created store simply has none, and that is not itself a problem."""
    path = hub / MARKER
    if not path.exists():
        return None, ""
    try:
        return parse_marker(path.read_text()), ""
    except (OSError, ValueError) as e:
        return None, f"{path} is unreadable ({e})"


def enclosing_repo(
    hub: Path, runner: GitRunner, timeout: float = GIT_TIMEOUT_SECONDS
) -> str | None:
    """Toplevel of a git repo *above* `hub`, or None.

    Asked from `hub.parent`, so the hub's own repo is never the answer: anything found
    from there encloses the hub rather than being it.
    """
    parent = hub.parent
    if not parent.is_dir() or parent == hub:
        return None
    result = runner(["-C", str(parent), "rev-parse", "--show-toplevel"], LOCAL_ENV, timeout)
    return result.stdout.strip() or None if result.returncode == 0 else None


def inspect_hub(
    hub: Path, runner: GitRunner = default_git_runner, timeout: float = GIT_TIMEOUT_SECONDS
) -> HubState:
    """Read the hub store's durability-relevant state. Never raises."""
    if not hub.is_dir():
        return HubState()
    if not (hub / ".git").exists():
        return HubState(exists=True, enclosing_repo=enclosing_repo(hub, runner, timeout))
    created, marker_error = read_marker(hub)
    try:
        dirty, inspect_error = tuple(dirty_paths(hub, runner, timeout)), ""
    except HubCommitError as e:
        dirty, inspect_error = (), str(e)
    return HubState(
        exists=True,
        is_repo=True,
        created=created,
        marker_error=marker_error,
        remotes=tuple(remotes(hub)),
        commits=commit_count(hub),
        backlog=read_backlog(default_backlog_path(hub)),
        dirty=dirty,
        inspect_error=inspect_error,
        enclosing_repo=enclosing_repo(hub, runner, timeout),
    )


def assess(
    now: datetime,
    runner: GitRunner = default_git_runner,
    *,
    environ: Mapping[str, str] = os.environ,
    home: Path = Path.home(),
    repo_root: Path = REPO_ROOT,
    secrets_dir: Path = SECRETS_DIR,
    creds: Path = CREDS,
    env_dir: Path = DEPLOY_ENV_DIR,
    scaffold_dir: Path = HUB_SCAFFOLD_DIR,
    hub_key: Path = HUB_KEY,
    is_tracked: Callable[[Path], bool] = _git_tracked,
) -> tuple[HubTarget, Findings]:
    """Gather everything and return the verdict for this instance.

    Every location this reads is a keyword argument defaulting to the module constant,
    so the *composition* — which check is fed which path — is testable against planted
    violations under a tempdir. The deployment passes none of them and gets the real
    layout; a test that plants a leak and drives this function proves the wiring, which
    no amount of coverage on the individual checks can.
    """
    target = resolve_hub_target(environ.get(ENV_VAR), home, repo_root)
    secret_files = sorted(secrets_dir.glob("*.env")) if secrets_dir.is_dir() else []
    repo_secrets = _repo_secret_candidates(env_dir)

    fatal = fatal_problems(
        secret_files=secret_files,
        repo_secrets=repo_secrets,
        creds=creds,
        hub_credentials=[hub_key],
        hub_content=hub_checkout_problems(scaffold_dir),
        hub_path_problem=target.problem,
        origin_violations=origin_credential_violations(repo_root, runner),
        is_tracked=is_tracked,
    )

    # A refused hub path is not inspected: the warnings describe a store that must not
    # be used at all, and reading it would only bury the fatal finding in noise.
    state = inspect_hub(target.path, runner) if target.path and not target.problem else HubState()
    notes = notes_for(target.path if not target.problem else None, state)
    if not secret_files:
        notes.append(
            f"no local secret files in {secrets_dir} — fine for a managed-connector-only "
            f"setup; credentials.json perms are still checked"
        )
    return target, Findings(
        tuple(fatal),
        tuple(warnings_for(target.path, state, now)),
        tuple(notes),
    )


def report(target: HubTarget, findings: Findings) -> int:
    """Print the verdict and return the exit code. 1 only for leak conditions.

    Split out from `main` so the "a warning must not block start" guarantee is something
    a test can assert directly, rather than something the reader has to trust.
    """
    print(f"secrets-preflight: hubs resolve to {target.description}")
    for note in findings.notes:
        print(f"secrets-preflight: {note}", file=sys.stderr)
    # Warnings are durability conditions and exit 0 by design. With StartLimitBurst=5,
    # failing here would turn "knowledge is not backed up" into "the assistant is gone".
    for warning in findings.warnings:
        print(f"secrets-preflight WARN: {warning}", file=sys.stderr)
    if findings.blocks_start:
        for problem in findings.fatal:
            print(f"secrets-preflight FAIL: {problem}", file=sys.stderr)
        return 1
    suffix = (
        f" ({len(findings.warnings)} warning(s) above — durability only, start not blocked)"
        if findings.warnings
        else ""
    )
    print(f"secrets-preflight OK{suffix}")
    return 0


def main(argv=None, **overrides: Any) -> int:
    """The `ExecStartPre` entry point: exit 1 blocks service start.

    `overrides` forwards `assess`'s injection seams and exists so a test can drive this
    exact path — main -> assess -> report — against planted violations. The deployment
    passes none, so what systemd runs is what the tests run.
    """
    return report(*assess(datetime.now(UTC), **overrides))


if __name__ == "__main__":
    raise SystemExit(main())
