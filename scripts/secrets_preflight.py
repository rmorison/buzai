"""The deployment's structural preflight: refuse to start on a layout that can leak.

Wired as the systemd `ExecStartPre` and as the smoke test's SC0 gate. Pre-commit hooks
are a `make dev` developer target and are verified absent from deployments, so this is
the *one* control that actually runs there — which makes it the place the layout
guarantees (R3, R15/R16/R17) become enforceable rather than conventional.

The fatal / warning split — the whole point of this module
----------------------------------------------------------
`ExecStartPre` failure blocks service start, and the unit retries a failed start every
minute for as long as the check keeps failing. So a check that fails here does not
degrade the assistant, it keeps it off the air until the condition is fixed. Which
conditions earn that is therefore a deliberate decision, not a matter of severity feel:

**FATAL** — leak conditions. Personal state is somewhere it can reach the public repo,
or the instance has a way to push to it. Refusing to run is strictly better than
running:

  * `hubs/` in the checkout holds anything beyond the named scaffolds (R16) — whether
    or not the public repo already tracks it, which makes it worse, not exempt;
  * a real `.env` sits under `deploy/env/` (R17);
  * the resolved hub path is inside — or contains — the checkout (R3);
  * the hub push credential is not 0600, or is tracked by *any* git repo (R15);
  * a credential helper is configured for the **public** origin (R4/R3);
  * any other GitHub account credential (#29): a remote URL with a token in it, pushes
    to origin redirected by `pushurl` or a URL rewrite, an `http.extraHeader` carrying
    an Authorization header for GitHub, `core.askPass` or `GIT_ASKPASS`,
    a `~/.netrc` entry or a `gh` login for GitHub or the origin's host, or `GH_TOKEN`,
    `GITHUB_TOKEN` or `GITHUB_PERSONAL_ACCESS_TOKEN` in the service environment.

**WARNING** — durability conditions. Knowledge is being recorded but is not (yet)
safely off-box. Nothing is leaking, and the assistant still works:

  * a hub repo older than `REMOTE_GRACE_SECONDS` with no remote configured;
  * an unpushed backlog (count and age of the oldest commit);
  * a dirty hub working tree;
  * a git repository *above* the hub directory — a dotfiles repo at `~` makes `~/hubs`
    a nested repo whose content the outer repo could swallow. That outer repo is the
    owner's own and of unknown visibility, so it is not proof of a leak the way the
    public checkout is; and failing here would take down every instance whose home
    directory is version-controlled;
  * untracked, non-ignored files in the public checkout, which is the assistant's working
    directory: each is one `git add -A` from a commit, but not yet a leak (#29).

Making any of those fatal converts degraded knowledge into a total assistant outage,
which is the trade this design explicitly refuses. That refusal is enforced rather than
intended: `durability_report` wraps the whole warning half in a catch-all, so an
*unexpected* exception on that path — the class of bug a stored naive timestamp used to
be — degrades to a warning and exit 0 instead of propagating out of `ExecStartPre`. The
fatal half is pointedly left outside that wrapper: a leak check that cannot answer must
stay unverified-and-fatal, never be softened into a warning.

Not a no-op, by construction
----------------------------
The original tracked-check in this file was fed only paths *outside* the repo, so it
could never fire — a dead check shipping with full confidence
(`docs/solutions/architecture-patterns/running-claude-code-always-on.md`). Every check
below is therefore fed paths that can actually fail, and every one has a test that
plants the violation and proves it fires. `_git_tracked` in particular now runs from
the file's **own** directory so git discovers whatever repo encloses it, which is the
only way "tracked by any repo" can be true of `~/.ssh/buzai-hub`.

Every git call is bounded, and times out in the direction its check demands
---------------------------------------------------------------------------
`ExecStartPre` runs this on every start, and git has no timeout of its own — while the
paths involved (`~/.ssh`, the hub store) can be network mounts. An unbounded call is
therefore a hung start, and a start that hangs on every retry keeps the assistant off
the air for as long as the stall lasts. So every call carries an explicit timeout and
every timeout degrades the way the split above dictates:

  * **leak checks fail closed.** `_git_tracked` returns a reason string instead of
    `False`, and `tracked_paths` raises; both become fatal "unverified rather than
    clean" findings. A leak check that times out must never read as a pass.
  * **durability checks warn.** `commit_count` and `remotes` raise instead of returning
    0 / `()` — values that mean "nothing to lose yet" and "nothing has gone off-box" —
    and `inspect_hub` turns that into a warning, so service start is not blocked by a
    slow disk.

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

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

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
    GIT,
    MARKER,
    SCAFFOLDS,
    HubInitError,
    commit_count,
    parse_marker,
    plan_migration,
    remotes,
    tracked_paths,
)
from scripts.hub_paths import (  # noqa: E402
    ENV_VAR,
    HubPathError,
    expand,
    main_checkout,
    refusal,
    source_of,
)
from scripts.hub_remote import (  # noqa: E402
    LOCAL_ENV,
    SCP_LIKE,
    GitResult,
    GitRunner,
    config_url,
    default_git_runner,
    origin_credential_violations,
    url_credential_problem,
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
    """Secrets a git repo carries. Pure given `is_tracked`.

    `is_tracked` answers True / False / a *reason string* meaning "could not tell" —
    the shape `_git_tracked` returns when git timed out or could not be run. That third
    answer is a finding of its own rather than a `False`: this is a leak check, and the
    one thing it must never do is report "cannot tell" as "clean".
    """
    probs = []
    for p in paths:
        answer = is_tracked(p)
        if isinstance(answer, str):
            probs.append(
                f"cannot determine whether {p} is tracked by git — treated as unverified "
                f"rather than clean: {answer}"
            )
        elif answer:
            probs.append(f"{p} is tracked by git (secrets must be untracked)")
    return probs


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


# The one thing an operator hitting this needs to know and cannot guess: `make hub-init`
# is not only for a fresh instance. The state that produces this finding most often is a
# migration interrupted half-way — hub repo already there, content still here — and the
# remedy is the same verb, which resumes. Saying "run `make hub-init`" without this reads
# as advice that was already followed and did nothing.
RESUMES = (
    " — `make hub-init` moves it out of the working tree AND the index, and finishes an "
    "interrupted migration, so run it even though the hub repo already exists"
)


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
            f"store"
            if rel in staged
            else f"{source / rel} is personal hub content inside the public checkout — hub "
            f"content lives in the private hub repo"
        )
        + RESUMES
        for rel in content
    ]


def resolve_hub_target(raw: str | None, home: Path, repo_root: Path) -> HubTarget:
    """Where hubs resolve to and whether that is fatal. Never raises.

    The resolver's own entry point raises, which would abort the preflight before the
    secrets and credential checks ran. Here a bad hub path is one finding among several,
    and the resolved path is printed either way so a mismatch is visible in the journal.

    Run from a linked worktree, the main checkout it belongs to is public too, so the
    hub path is refused inside either one. A main checkout that can't be found is a
    refusal of its own, fatal like the rest: "could not rule it out" is not "clean".
    """
    try:
        path = expand(raw, home).resolve()
    except HubPathError as e:
        return HubTarget(None, f"unresolvable ({e})", str(e))
    description = f"{path} (from {source_of(raw)})"
    root = repo_root.resolve()
    try:
        main_root = main_checkout(root)
    except HubPathError as e:
        return HubTarget(path, description, str(e))
    except (OSError, ValueError) as e:
        # e.g. an unreadable `.git`, or git printing a path that isn't UTF-8: still a
        # refusal, never a crash that keeps every other check from running
        return HubTarget(
            path,
            description,
            f"cannot tell whether {root} is a linked worktree whose main checkout holds "
            f"the hub path — treated as unverified rather than clean: {type(e).__name__}: {e}",
        )
    return HubTarget(path, description, refusal(path, root, main_root))


def fatal_problems(
    *,
    secret_files: Sequence[Path],
    repo_secrets: Sequence[Path],
    creds: Path,
    hub_credentials: Sequence[Path],
    hub_content: Sequence[str],
    hub_path_problem: str | None,
    origin_violations: Sequence[str],
    is_tracked: Callable[[Path], bool | str],
    account_violations: Sequence[str] = (),
) -> list[str]:
    """Every leak condition, in one pure function. Blocks service start.

    `hub_credentials` goes through the same perms + tracked pair as the secrets files:
    a deploy key that can write the private hub repo is a secret like any other, and a
    dotfiles repo that tracks it publishes push access to the knowledge base.
    `account_violations` are the GitHub account credentials `account_credential_violations`
    found (#29); `assess()` always passes them, and a composition test fails if it stops.
    """
    return [
        *problems(secret_files, repo_secrets, creds, is_tracked),
        *env_file_problems(repo_secrets),
        *hub_content,
        *perm_problems(hub_credentials),
        *tracked_problems([p for p in hub_credentials if Path(p).exists()], is_tracked),
        *([hub_path_problem] if hub_path_problem else []),
        *origin_violations,
        *account_violations,
    ]


# --- pure: GitHub account credentials (#29) ----------------------------------------
#
# The credential-helper check above asks one question. A deployment can still publish
# to GitHub by routes that need no helper: a token in a remote URL, a push redirected to
# another URL, a `~/.netrc` entry, a `gh` login, a token in the service environment, or
# an askpass program. Each needs a credential someone added to the box by hand, and the
# policy (`deploy/README.md`) is that the service account holds none but the hub deploy
# key. These deciders judge what the gatherer read, and every message names the KIND of
# credential and where it is — never a URL's userinfo, a token, or a rewrite's base URL,
# because these messages go to the journal.

# The environment variables `gh` and the GitHub MCP server read a token from. Presence
# is the finding; the value is never read into a message.
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN")

GITHUB_HOST = "github.com"

# `remote.<name>.url` / `remote.<name>.pushurl`. The name may itself contain dots.
REMOTE_URL_KEY = re.compile(r"^remote\.(?P<name>.+)\.(?P<key>url|pushurl)$", re.IGNORECASE)
# `url.<base>.insteadOf` / `url.<base>.pushInsteadOf`. The base is never printed: it can
# hold a token.
REWRITE_KEY = re.compile(r"^url\..+\.(?P<kind>pushinsteadof|insteadof)$", re.IGNORECASE)

# Keywords in a `.netrc` that take one value. Skipping that value is what keeps a
# password that happens to read "machine" from being taken for a host.
NETRC_VALUED = frozenset({"login", "password", "account", "port"})


def is_network_url(url: str) -> bool:
    """Whether git would reach another machine with this push URL. Pure.

    A push URL that names no network transport (the `no_push` hardening idiom, a local
    path, `file://`) can't publish anything, so a redirect to it is not a finding. Any
    other scheme counts as network, not only git's named transports: git also accepts
    `git+ssh://` and `ssh+git://`, and a `<helper>://` URL hands the push to a remote
    helper. On a leak check an unknown scheme fails closed.
    """
    scheme = urlsplit(url).scheme.lower()
    if scheme:
        return scheme != "file"
    return bool(SCP_LIKE.match(url.strip()))


def url_host(url: str) -> str | None:
    """The host a remote URL names, lower-cased, or None. Pure. Accepts scp-like form."""
    try:
        host = urlsplit(config_url(url)).hostname
    except ValueError:
        return None
    return host.lower() if host else None


def is_github_host(host: str, origin_host: str | None) -> bool:
    """A host a GitHub account credential would be for. Pure.

    `github.com` is always in the set, so a missing or local origin can't silence the
    `.netrc` and `gh` checks; `api.github.com` and other subdomains are in it too, since a
    token for the API posts without `gh`.
    """
    host = host.lower().rstrip(".")
    return (
        host == GITHUB_HOST
        or host.endswith("." + GITHUB_HOST)
        or (origin_host is not None and host == origin_host)
    )


def remote_url_problems(entries: Iterable[tuple[str, str]], repo_root: Path) -> list[str]:
    """Remote URLs in the checkout's config that embed a credential. Pure. #29 R2.

    `entries` are `(config key, value)` pairs for every remote's `url` and `pushurl`.
    Every remote counts, not only origin: a second remote with a token in it pushes this
    checkout just as well. The value is judged, never printed.
    """
    found = []
    for key, value in entries:
        match = REMOTE_URL_KEY.match(key)
        if not match or not url_credential_problem(value):
            continue
        name = match.group("name")
        found.append(
            f"remote.{name}.{match.group('key').lower()} embeds a credential in its URL — "
            f"that is a way to push this checkout, and git echoes the URL on failure, so "
            f"the token would also reach the journal; set the URL again without it "
            f"(`git -C {repo_root} remote set-url {name} <url without user:token@>`) and "
            f"revoke the token"
        )
    return found


def _label_match(pattern: str, host: str) -> bool:
    """git's urlmatch host rule: same number of labels, each equal or `*`."""
    pat, got = pattern.split("."), host.split(".")
    return len(pat) == len(got) and all(p in ("*", g) for p, g in zip(pat, got, strict=True))


def header_scope_match(scope: str, url: str) -> tuple[int, int, int] | None:
    """How specifically an `http.<scope>.*` key matches `url`, or None. Pure.

    Follows git's urlmatch closely enough to order entries: scheme, host (per label,
    `*` allowed) and port must match, the scope's user (if any) must be the URL's, and
    the scope's path must be a prefix of the URL's at a `/` boundary. The tuple orders
    like git's own comparison: host characters matched, then path length, then whether
    a user was named. An unscoped key matches every URL with the lowest specificity.
    """
    if not scope:
        return (0, 0, 0)
    try:
        want, have = urlsplit(scope), urlsplit(url)
        want_port, have_port = want.port, have.port
    except ValueError:
        return None
    if want.scheme.lower() != have.scheme.lower():
        return None
    want_host, have_host = (want.hostname or ""), (have.hostname or "")
    if not want_host or not _label_match(want_host, have_host):
        return None
    defaults = {"http": 80, "https": 443}
    scheme = have.scheme.lower()
    if (want_port or defaults.get(scheme)) != (have_port or defaults.get(scheme)):
        return None
    if want.username and want.username != have.username:
        return None
    prefix = want.path.rstrip("/")
    path = have.path or "/"
    if prefix and not (path == prefix or path.startswith(prefix + "/")):
        return None
    host_chars = len(want_host.replace("*", ""))
    return (host_chars, len(prefix), 1 if want.username else 0)


def headers_sent(entries: Sequence[tuple[str, str]], url: str) -> list[tuple[str, str]]:
    """`(scope, header)` pairs git would send to `url` from `http.*.extraHeader`. Pure.

    git reads the entries in config order. An entry that matches the URL less
    specifically than the best one seen so far is ignored, and an empty value clears
    the list built so far (how a local config cancels a global header). Checked against
    git 2.34.1 with a recording proxy, in the review of #35.
    """
    best: tuple[int, int, int] | None = None
    sent: list[tuple[str, str]] = []
    for key, value in entries:
        scope = key[len("http.") : -len(".extraheader")]
        rank = header_scope_match(scope, url)
        if rank is None or (best is not None and rank < best):
            continue
        best = rank
        if value == "":  # only an exactly empty value resets; git sends " " as a header
            sent.clear()
        else:
            sent.append((scope, value))
    return sent


def extra_header_problems(
    entries: Iterable[tuple[str, str]],
    origin_host: str | None,
    repo_root: Path,
    origin_urls: Sequence[str] = (),
) -> list[str]:
    """`http.extraHeader` entries that send an Authorization header to GitHub. Pure.

    git sends `http.<url>.extraHeader` with every request to a matching URL, and an
    `Authorization:` header there is a push credential with no helper configured. It is
    the shape `actions/checkout` persists into a clone, so a checkout copied from CI
    carries it.

    What counts is what git would actually send: `headers_sent` is asked for each https
    URL the instance could push to (origin's own URLs, and each scope on a GitHub host
    or the origin's host, which stands for a direct push there). Only an Authorization
    header counts: a header for another service is not a GitHub credential, and a false
    fatal is an outage. The key's URL and the value are never printed.
    """
    keyed = [
        (key, value)
        for key, value in entries
        if key.lower().startswith("http.") and key.lower().endswith(".extraheader")
    ]
    targets = []
    for url in origin_urls:
        try:
            if urlsplit(url).scheme.lower() in ("http", "https"):
                targets.append(url)
        except ValueError:
            continue  # not a URL git could send a header to
    for key, _ in keyed:
        scope = key[len("http.") : -len(".extraheader")]
        host = url_host(scope) if scope else None
        if host and is_github_host(host.replace("*", "x"), origin_host):
            targets.append(scope)
    if any(not key[len("http.") : -len(".extraheader")] for key, _ in keyed):
        targets.append("https://" + GITHUB_HOST + "/")
    shapes = set()
    for url in targets:
        for scope, header in headers_sent(keyed, url):
            if header.split(":", 1)[0].strip().lower() == "authorization":
                shapes.add("http.<url>.extraHeader" if scope else "http.extraHeader")
    return [
        f"an {shape} entry sends an Authorization header to GitHub — git adds it to "
        f"every push with no credential helper configured; remove it (find it with "
        f"`git -C {repo_root} config --show-origin --name-only --get-regexp "
        f"'^http\\..*extraheader$'`) and revoke the token"
        for shape in sorted(shapes)
    ]


def push_route_problems(
    origin: str,
    push_urls: Sequence[str],
    mechanisms: Sequence[str],
    repo_root: Path,
    *,
    origin_key_flagged: bool,
) -> list[str]:
    """Pushes to origin that go somewhere `remote.origin.url` doesn't say. Pure. #29 R3.

    `push_urls` is what `git remote get-url --push --all origin` reports, so git has
    already applied `pushurl`, `pushInsteadOf` and `insteadOf` its own way; only the
    effect is judged here, not the presence of a rewrite rule. `mechanisms` names what
    did it, by key shape only. `origin_key_flagged` says R2 already reported a token in
    origin's own keys, so the same token is not reported twice.
    """
    found = []
    if not origin_key_flagged and any(url_credential_problem(u) for u in push_urls):
        found.append(
            "origin's effective push URL embeds a credential after git applies its URL "
            "rewrites — a `url.<base>.insteadOf` or `pushInsteadOf` rule puts a token into "
            f"every push to the public repo; find it with `git -C {repo_root} config "
            "--show-origin --name-only --get-regexp '^url\\.'`, remove it and revoke the "
            "token"
        )
    redirected = [u for u in push_urls if u != origin and is_network_url(u)]
    if redirected:
        how = ", ".join(mechanisms) if mechanisms else "a push URL rewrite"
        found.append(
            f"pushes to origin are redirected away from remote.origin.url (by {how}) — "
            f"this instance must have no way to push to the public repo, and a redirect "
            f"is how one is added without a credential helper; remove it (`git -C "
            f"{repo_root} config --unset-all remote.origin.pushurl`, or the rewrite rule "
            f"shown by `git -C {repo_root} config --show-origin --get-regexp '^url\\.'`)"
        )
    return found


def rewrite_mechanisms(entries: Iterable[tuple[str, str]], origin: str) -> list[str]:
    """Which redirects apply to origin, named by key shape. Pure.

    `entries` are `(key, value)` pairs for `remote.origin.pushurl` and every
    `url.<base>.(push)insteadOf`. A rewrite counts only when its value is a prefix of the
    origin URL, so a rule for an unrelated host is not blamed. The base is never named.
    """
    shapes: list[str] = []
    for key, value in entries:
        lowered = key.lower()
        if lowered == "remote.origin.pushurl":
            shape = "remote.origin.pushurl"
        else:
            match = REWRITE_KEY.match(key)
            if not match or not value or not origin.startswith(value):
                continue
            shape = (
                "a url.<base>.pushInsteadOf rewrite"
                if match.group("kind").lower() == "pushinsteadof"
                else "a url.<base>.insteadOf rewrite"
            )
        if shape not in shapes:
            shapes.append(shape)
    return shapes


def netrc_hosts(text: str) -> tuple[set[str], bool]:
    """(`machine` names, whether a `default` entry exists) in a `.netrc`. Pure. #29 R4.

    Tokenized by hand rather than with the stdlib `netrc` module, which reads every
    password into memory and whose parse errors can quote the file. Only the token after
    `machine` is kept. A `macdef` body runs to the next blank line and is skipped, so a
    macro that mentions a host can't raise a false fatal — which would be an outage.
    """
    hosts: set[str] = set()
    has_default = False
    expect: str | None = None
    in_macdef = False
    for line in text.splitlines():
        if in_macdef:
            in_macdef = bool(line.strip())
            continue
        if line.lstrip().startswith("#"):
            continue
        for token in line.split():
            if expect == "machine":
                hosts.add(token.strip("\"'").lower())
                expect = None
            elif expect == "value":
                expect = None
            elif expect == "macdef":
                expect = None
                in_macdef = True
                break
            elif token == "machine":
                expect = "machine"
            elif token == "default":
                has_default = True
            elif token in NETRC_VALUED:
                expect = "value"
            elif token == "macdef":
                expect = "macdef"
    return hosts, has_default


def gh_hosts(text: str) -> set[str]:
    """The top-level keys of a `gh` `hosts.yml`, lower-cased. Pure. #29 R5.

    Read line by line (column 0, not a comment, quotes stripped, the text before the
    first `:`), so no YAML dependency is added and no value is read. Newer `gh` keeps the
    token in the system keyring but still writes the host entry, so the entry is the
    signal.
    """
    hosts = set()
    for line in text.splitlines():
        if not line or line[0] in " \t#-" or ":" not in line:
            continue
        key = line.split(":", 1)[0].strip().strip("\"'").lower()
        if key:
            hosts.add(key)
    return hosts


def netrc_problems(
    path: Path, hosts: Iterable[str], has_default: bool, origin_host: str | None
) -> list[str]:
    """`.netrc` entries that would hand git or an API client a GitHub credential. Pure."""
    found = [
        f"{path} has a `machine` entry for {host} — a stored password or token there lets "
        f"this instance push to or post on GitHub with no credential helper; remove the "
        f"entry and revoke the token"
        for host in sorted(h for h in hosts if is_github_host(h, origin_host))
    ]
    if has_default:
        found.append(
            f"{path} has a `default` entry, which applies to every host including GitHub — "
            f"remove it; the service account holds no GitHub account credential"
        )
    return found


def gh_login_problems(path: Path, hosts: Iterable[str], origin_host: str | None) -> list[str]:
    """`gh` logins that would let this instance post to GitHub. Pure. #29 R5."""
    return [
        f"the gh CLI is logged in to {host} ({path}) — that gives this instance "
        f"`gh issue create`/`comment` on public repos with no credential helper; run "
        f"`gh auth logout --hostname {host}` as the service account"
        for host in sorted(h for h in hosts if is_github_host(h, origin_host))
    ]


def token_env_problems(environ: Mapping[str, str]) -> list[str]:
    """GitHub tokens and askpass programs in the preflight's environment. Pure. #29 R6.

    Presence only. Under systemd this is the service environment, since `ExecStartPre`
    gets the unit's `Environment=` and `EnvironmentFile=`. `SSH_ASKPASS` is deliberately
    not checked: some distributions set it system-wide for desktop sessions.
    """
    found = [
        f"{var} is set in the service environment — `gh` and the GitHub MCP server read a "
        f"token from it and can post publicly; remove it from the unit's Environment= / "
        f"EnvironmentFile= and revoke the token"
        for var in TOKEN_VARS
        if environ.get(var, "").strip()
    ]
    if environ.get("GIT_ASKPASS", "").strip():
        found.append(
            "GIT_ASKPASS is set in the service environment — git asks that program for a "
            "push credential with no helper configured, and a headless service account "
            "has no use for one; remove it from the unit's environment"
        )
    return found


def askpass_problem(repo_root: Path) -> str:
    return (
        f"core.askPass is configured for this checkout — git asks that program for a push "
        f"credential with no helper configured, and a headless service account has no "
        f"use for one; remove it with `git -C {repo_root} config --unset core.askPass` "
        f"(or from the global config it came from)"
    )


def untracked_warning(names: Sequence[str], repo_root: Path) -> str | None:
    """Untracked, non-ignored files in the checkout, by name. Pure. #29 R8.

    A warning, never fatal: a stray scratch file is not yet a leak, and failing start on
    one would take the assistant down for a file it may well have written itself. But
    the assistant's working directory is the public checkout, so each is one
    `git add -A` from a commit.
    """
    if not names:
        return None
    shown = ", ".join(names[:5])
    more = f" and {len(names) - 5} more" if len(names) > 5 else ""
    return (
        f"{len(names)} untracked file(s) in the public checkout {repo_root} that "
        f".gitignore does not cover ({shown}{more}) — each is one `git add -A` from a "
        f"commit to the public repo; move scratch work out of the checkout, or delete it"
    )


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

    Reported at *any* size: a deploy key that was removed or rotated, or a network that
    is down for days, makes a silent push failure an expected steady state rather than
    an edge case, and service start is the one moment the operator reliably reads the
    journal.
    """
    if state.backlog.count == 0:
        return None
    age = state.backlog.oldest_age_seconds(now)
    oldest = f", oldest {_age(age)} old" if age is not None else ""
    reason = f" — last error: {state.backlog.last_error}" if state.backlog.last_error else ""
    return (
        f"{state.backlog.count} hub commit(s) have not reached the remote{oldest}{reason}; "
        f"`make hub-push` retries (a removed or rotated hub deploy key is the usual cause)"
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


def credential_warning(text: str | None, creds: Path) -> str | None:
    """Whether the stored Claude credentials can still authenticate. Pure.

    The perms check above asks whether this file is *safe*; this asks whether it still
    *works*. They are different questions, and only the first was ever asked — which is
    how a live instance sat dead for nine days: the OAuth tokens were emptied in place,
    the file stayed present and 0600, the preflight stayed green, and every start failed
    at `claude remote-control` with "You must be logged in". Nothing said so.

    A WARNING, never fatal. A logged-out instance is already going to fail at ExecStart;
    failing here too would only add a second way to die, and if this check ever misread a
    working file, a fatal would keep a healthy assistant off the air until someone fixed
    it. The value of this check is that `make doctor` and the journal can name the cause
    instead of leaving an operator to infer it from a restart loop.

    Never reports a token's value or length — only that one is missing.
    """
    if text is None:
        return (
            f"no readable Claude credentials at {creds} — the service will start but "
            f"never register; run `make auth` (see docs/SETUP.md §3)"
        )
    try:
        data = json.loads(text)
    except ValueError:
        return (
            f"{creds} is not readable as JSON, so the stored login cannot be used — "
            f"the service will start but never register; re-run `make auth`"
        )
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        # An auth shape this does not recognize. Saying nothing beats guessing: a wrong
        # warning here teaches the operator to ignore the one that matters.
        return None
    if any(str(oauth.get(k) or "").strip() for k in ("accessToken", "refreshToken")):
        return None
    return (
        f"{creds} holds no usable token — accessToken and refreshToken are both empty, "
        f"so the service will start but never register. This is what a revoked or "
        f"cleared login looks like from disk; run `make auth`"
    )


def read_credentials(creds: Path) -> str | None:
    """The credentials file's text, or None when absent or unreadable. Side-effecting."""
    try:
        return creds.read_text()
    except OSError:
        return None


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


def _git_tracked(path, *, git_bin: str = GIT, timeout: float = GIT_TIMEOUT_SECONDS) -> bool | str:
    """Is `path` tracked by whatever git repo encloses it — not merely by this checkout?

    True / False, or a *reason string* when git could not answer, which
    `tracked_problems` turns into an unverified-and-therefore-fatal finding. Silently
    returning False there would be the worst kind of dead check: a leak test that passes
    because it never ran.

    Run from the file's **own** directory so git discovers the enclosing repository by
    walking up. `git -C REPO_ROOT` could never see `~/.ssh/buzai-hub` in a dotfiles repo
    at `~`, and a check that cannot see its target is the dead no-op this file already
    shipped once. That reach is also why the call is bounded: the directory is the
    target's, which may be a network mount, and this runs on every `ExecStartPre`.
    A nonzero returncode (no repo, path outside it, untracked) is a value, not an error.
    """
    p = Path(path)
    parent = p.parent if p.parent.is_dir() else REPO_ROOT
    argv = [git_bin, "-C", str(parent), "ls-files", "--error-unmatch", "--", str(p)]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"`git ls-files` in {parent} timed out after {timeout}s"
    except OSError as e:
        return f"cannot run {git_bin!r} in {parent}: {e}"
    return r.returncode == 0


def hub_checkout_problems(
    source: Path = HUB_SCAFFOLD_DIR, git_bin: str = GIT, timeout: float = GIT_TIMEOUT_SECONDS
) -> list[str]:
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
        tracked = tracked_paths(source, git_bin, timeout)
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
    hub: Path,
    runner: GitRunner = default_git_runner,
    timeout: float = GIT_TIMEOUT_SECONDS,
    git_bin: str = GIT,
) -> HubState:
    """Read the hub store's durability-relevant state. Never raises.

    Every reader here is bounded, and a reader that could not answer lands in
    `inspect_error` — which `warnings_for` reports and which does *not* block start.
    Durability is the whole subject of this function, so a git that hung must warn; it
    must not be absorbed into a zero commit count that silences the stale-remote warning.
    """
    if not hub.is_dir():
        return HubState()
    if not (hub / ".git").exists():
        return HubState(exists=True, enclosing_repo=enclosing_repo(hub, runner, timeout))
    created, marker_error = read_marker(hub)
    errors: list[str] = []
    try:
        dirty: tuple[str, ...] = tuple(dirty_paths(hub, runner, timeout))
    except HubCommitError as e:
        dirty = ()
        errors.append(str(e))
    try:
        remote_names: tuple[str, ...] = tuple(remotes(hub, git_bin, timeout))
        commits = commit_count(hub, git_bin, timeout)
    except HubInitError as e:
        remote_names, commits = (), 0
        errors.append(str(e))
    return HubState(
        exists=True,
        is_repo=True,
        created=created,
        marker_error=marker_error,
        remotes=remote_names,
        commits=commits,
        backlog=read_backlog(default_backlog_path(hub)),
        dirty=dirty,
        inspect_error="; ".join(errors),
        enclosing_repo=enclosing_repo(hub, runner, timeout),
    )


def durability_report(
    target: HubTarget,
    runner: GitRunner,
    now: datetime,
    grace_seconds: float = REMOTE_GRACE_SECONDS,
) -> tuple[list[str], list[str]]:
    """(notes, warnings) for the WARNING half of the split. **Never raises.**

    The catch-all is the point, and it is deliberately not a `try` around the whole of
    `assess()`. Each individual reader below already degrades correctly for the failures
    it anticipates — a git that hangs, a marker that will not parse — and every one of
    those has a test. This is the backstop for the failure nobody anticipated: an
    exception type the readers do not catch, raised somewhere on the durability path.
    `ExecStartPre` failure blocks service start, and a bug fails every retry the same
    way, so the class of bug that reaches here — a `TypeError` from a stored timestamp,
    an unexpected `AttributeError` in a future reader — would convert "knowledge is not
    backed up" into "the assistant is down until someone fixes the bug". That is the
    exact trade the fatal/warning split exists to refuse, and it must not be reachable
    by accident.

    The fatal path gets **no** such treatment, and must not: `fatal_problems`,
    `hub_checkout_problems` and `origin_credential_violations` stay outside it, so a leak
    check that cannot answer is still an unverified-and-therefore-fatal finding rather
    than a warning. Failing open on durability is safe; failing open on a leak is the one
    thing this module exists to prevent.
    """
    hub = target.path if target.path and not target.problem else None
    try:
        state = inspect_hub(hub, runner) if hub is not None else HubState()
        return notes_for(hub, state), warnings_for(hub, state, now, grace_seconds)
    except Exception as e:  # noqa: BLE001 — see the docstring: this is the backstop
        return [], [
            f"the durability checks could not be completed ({type(e).__name__}: {e}) — this "
            f"is a bug, and it is reported as a warning on purpose: nothing here can leak, "
            f"so it must not be able to block service start. The backup state of "
            f"{hub or 'the hub store'} is UNKNOWN until it is fixed; run `make doctor`"
        ]


def _unverified(what: str, result: GitResult) -> str:
    """A leak check git could not answer: fatal, never clean. First stderr line only."""
    lines = result.stderr.strip().splitlines()
    detail = lines[0] if lines else f"git exited {result.returncode}"
    return f"cannot determine {what} — treated as unverified rather than clean: {detail}"


def _config_pairs(stdout: str) -> list[tuple[str, str]]:
    """`(key, value)` pairs from `git config -z --get-regexp` output."""
    pairs = []
    for entry in stdout.split("\0"):
        if entry:
            key, _, value = entry.partition("\n")
            pairs.append((key, value))
    return pairs


def git_credential_routes(
    repo_root: Path, runner: GitRunner, timeout: float = GIT_TIMEOUT_SECONDS
) -> tuple[list[str], str | None]:
    """(findings, origin host) for the routes git config can open. #29 R2, R3, R6a.

    `git config` exits 1 for "no such key" and anything else for "could not evaluate";
    as in `origin_credential_violations`, the two are kept apart, because folding them
    together is how a leak check becomes a silent pass. No origin at all has nothing to
    redirect; the credential-helper check already reports "this checkout" for it.
    """

    def git(*args: str) -> GitResult:
        return runner(["-C", str(repo_root), *args], LOCAL_ENV, timeout)

    found: list[str] = []
    listed = git("config", "-z", "--get-regexp", r"^remote\..*\.(url|pushurl)$")
    entries = _config_pairs(listed.stdout) if listed.returncode == 0 else []
    if listed.returncode not in (0, 1):
        found.append(
            _unverified("whether a remote URL in this checkout embeds a credential", listed)
        )
    found += remote_url_problems(entries, repo_root)
    origin_key_flagged = any(
        (m := REMOTE_URL_KEY.match(key))
        and m.group("name") == "origin"
        and url_credential_problem(v)
        for key, v in entries
    )

    origin_host = None
    origin_urls: list[str] = []
    got = git("config", "--get", "remote.origin.url")
    if got.returncode == 0 and got.stdout.strip():
        origin = got.stdout.strip()
        origin_host = url_host(origin)
        origin_urls.append(origin)
        push = git("remote", "get-url", "--push", "--all", "origin")
        if push.returncode == 0:
            origin_urls += [u.strip() for u in push.stdout.splitlines() if u.strip()]
        if push.returncode != 0:
            found.append(_unverified("where pushes to origin go", push))
        else:
            redirects = git(
                "config",
                "-z",
                "--get-regexp",
                r"^(remote\.origin\.pushurl|url\..*\.(pushinsteadof|insteadof))$",
            )
            # Only the NAME of the mechanism depends on this call; the finding itself comes
            # from git's own answer above, so a failure here leaves it generically named.
            mechanisms = (
                rewrite_mechanisms(_config_pairs(redirects.stdout), origin)
                if redirects.returncode == 0
                else []
            )
            found += push_route_problems(
                origin,
                [u.strip() for u in push.stdout.splitlines() if u.strip()],
                mechanisms,
                repo_root,
                origin_key_flagged=origin_key_flagged,
            )
    elif got.returncode not in (0, 1):
        found.append(_unverified("where pushes to origin go", got))

    headers = git("config", "-z", "--get-regexp", r"^http\.(.*\.)?extraheader$")
    if headers.returncode == 0:
        found += extra_header_problems(
            _config_pairs(headers.stdout), origin_host, repo_root, origin_urls
        )
    elif headers.returncode != 1:
        found.append(_unverified("whether an http.extraHeader carries a credential", headers))

    askpass = git("config", "--get", "core.askPass")
    if askpass.returncode == 0 and askpass.stdout.strip():
        found.append(askpass_problem(repo_root))
    elif askpass.returncode not in (0, 1):
        found.append(_unverified("whether core.askPass is configured", askpass))
    return found, origin_host


def gh_hosts_path(home: Path, environ: Mapping[str, str]) -> Path:
    """Where `gh` keeps its logins, by gh's own precedence."""
    if environ.get("GH_CONFIG_DIR"):
        return Path(environ["GH_CONFIG_DIR"]) / "hosts.yml"
    if environ.get("XDG_CONFIG_HOME"):
        return Path(environ["XDG_CONFIG_HOME"]) / "gh" / "hosts.yml"
    return home / ".config" / "gh" / "hosts.yml"


def _read_optional(path: Path) -> tuple[str | None, str | None]:
    """(text, error). Absent is (None, None): no file there is nothing to find."""
    try:
        return path.read_text(errors="replace"), None
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as e:
        # Includes a directory on the way that can't be searched: unverified, not a crash.
        return None, e.strerror or type(e).__name__


def account_file_problems(
    home: Path, environ: Mapping[str, str], origin_host: str | None
) -> list[str]:
    """GitHub credentials in the account's `.netrc` and `gh` login. #29 R4, R5."""
    found: list[str] = []
    netrc = home / ".netrc"
    text, error = _read_optional(netrc)
    if error:
        found.append(
            f"cannot read {netrc} to check it for a GitHub credential — treated as "
            f"unverified rather than clean: {error}"
        )
    elif text is not None:
        found += netrc_problems(netrc, *netrc_hosts(text), origin_host)
    hosts_file = gh_hosts_path(home, environ)
    text, error = _read_optional(hosts_file)
    if error:
        found.append(
            f"cannot read {hosts_file} to check for a gh login — treated as unverified "
            f"rather than clean: {error}"
        )
    elif text is not None:
        found += gh_login_problems(hosts_file, gh_hosts(text), origin_host)
    return found


def account_credential_violations(
    repo_root: Path,
    runner: GitRunner,
    *,
    home: Path,
    environ: Mapping[str, str],
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> list[str]:
    """Every GitHub account credential this instance holds, by kind. FATAL. #29.

    Leak checks, so outside `durability_report`'s catch-all: a check that cannot answer
    is an unverified finding, never a pass.
    """
    found, origin_host = git_credential_routes(repo_root, runner, timeout)
    return [
        *found,
        *account_file_problems(home, environ, origin_host),
        *token_env_problems(environ),
    ]


def untracked_report(
    repo_root: Path, runner: GitRunner, timeout: float = GIT_TIMEOUT_SECONDS
) -> str | None:
    """The untracked-files warning for the checkout. **Never raises.** #29 R8.

    A warning gatherer, so it holds to the warning half's rule: whatever goes wrong here
    degrades to a warning, and service start is never blocked by it.
    """
    try:
        listed = runner(
            [
                "-C",
                str(repo_root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "--directory",
                "--no-empty-directory",
                "-z",
            ],
            LOCAL_ENV,
            timeout,
        )
        if listed.returncode != 0:
            lines = listed.stderr.strip().splitlines()
            detail = lines[0] if lines else f"git exited {listed.returncode}"
            return f"cannot list untracked files in the public checkout {repo_root}: {detail}"
        return untracked_warning([n for n in listed.stdout.split("\0") if n], repo_root)
    except Exception as e:  # noqa: BLE001 — a warning gatherer must not block start
        return f"cannot list untracked files in the public checkout {repo_root}: {type(e).__name__}: {e}"


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
    is_tracked: Callable[[Path], bool | str] = _git_tracked,
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
        account_violations=account_credential_violations(
            repo_root, runner, home=home, environ=environ
        ),
    )

    # A refused hub path is not inspected: the warnings describe a store that must not
    # be used at all, and reading it would only bury the fatal finding in noise.
    notes, warnings = durability_report(target, runner, now)
    # Auth durability sits alongside hub durability: both are "this instance will stop
    # being useful", neither is a leak, and neither may block start.
    creds_warning = credential_warning(read_credentials(creds), creds)
    if creds_warning:
        warnings.append(creds_warning)
    stray = untracked_report(repo_root, runner)
    if stray:
        warnings.append(stray)
    if not secret_files:
        notes.append(
            f"no local secret files in {secrets_dir} — fine for a managed-connector-only "
            f"setup; credentials.json perms are still checked"
        )
    return target, Findings(tuple(fatal), tuple(warnings), tuple(notes))


def report(target: HubTarget, findings: Findings) -> int:
    """Print the verdict and return the exit code. 1 only for leak conditions.

    Split out from `main` so the "a warning must not block start" guarantee is something
    a test can assert directly, rather than something the reader has to trust.
    """
    print(f"secrets-preflight: hubs resolve to {target.description}")
    for note in findings.notes:
        print(f"secrets-preflight: {note}", file=sys.stderr)
    # Warnings are durability conditions and exit 0 by design. A fatal fails every retry
    # until the condition clears, so failing here would turn "knowledge is not backed up"
    # into "the assistant is down until someone fixes it".
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
