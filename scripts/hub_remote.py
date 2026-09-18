"""Refuse to push hub history anywhere that has not been *proven* private.

The hub repo (see `scripts/hub_init.py`) holds the principal's personal knowledge
base. R7 wants its history off-box; R3 wants personal state structurally incapable of
reaching a public repo. This module is the join between the two: nothing pushes until
the configured remote has been shown to be unreadable by an anonymous stranger.

How privacy is verified — and why not `gh`
------------------------------------------
By an **anonymous readability probe**: `git ls-remote` against the remote with every
credential suppressed. If that *succeeds*, the repository is publicly readable and the
push is refused. It is provider-agnostic and needs no new dependency.

`gh` was rejected on the merits, not on taste. `gh auth login` installs a **global**
`github.com` credential helper, which would hand this instance push access to the
*public* buzai origin and destroy the assumption the whole barrier rests on. And the
R4-compliant credential — an ssh deploy key scoped to the hub repo — has no API access
at all, so `gh repo view` could never succeed and a fail-closed rule would refuse every
push forever. The two halves were mutually exclusive.

Two layers of environment suppression, and why they differ
----------------------------------------------------------
Every network-facing git call in buzai — the anonymous probe, the authenticated probe,
and the pushes in `hub_commit` / `hub_review` — is built from `no_config_injection()`.
The anonymous probe alone adds a second layer on top. The split is deliberate, because
the two calls need *opposite* things from a credential.

**Layer 1, `no_config_injection()` — for every network-facing call.** These are git
*config injection* channels: they bypass `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`
entirely, so pointing those at `os.devnull` does not close them.

  * `GIT_CONFIG_COUNT` + every `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` pair,
    `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG` **unset**. One `url.<base>.insteadOf`
    entry rewrites the URL git actually contacts, which is fatal in *both* directions:
    it can send the anonymous probe somewhere readable (a private repo reads as
    public), and it can send the authenticated probe — or the push — to an
    attacker-chosen host that answers happily. The authenticated probe's *success* is
    what turns "every anonymous probe was denied" into PRIVATE, so a redirected auth
    probe manufactures the exact verdict that permits a push. The same channel installs
    a credential helper that a later `-c credential.helper=` cannot cancel. Pairs are
    enumerated from the environment rather than assumed to be few: the count is
    arbitrary.
  * `http_proxy` / `https_proxy` / `all_proxy` (both cases) and `GIT_PROXY_COMMAND`
    **unset**, plus `-c http.proxy=` on the command line (`NO_PROXY_ARGS`) — a proxy
    answers *for* the host, so it decides what the call sees. One that returns 200 for
    everything makes every repo look public (a refusal, survivable); one that returns a
    canned error page makes every repo look private (the failure this module exists to
    prevent); and one in front of a push sends hub content somewhere else entirely.

**Layer 2, the rest of `anonymous_env()` — the anonymous probe only.** A probe that
accidentally authenticates reports *private* for a *public* repo, so it must be unable
to obtain a credential at all:

  * `-c credential.helper=` — git's idiom for **resetting** the helper list, so no
    helper configured anywhere can run.
  * `GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` -> `os.devnull` — `~/.gitconfig` and
    `/etc/gitconfig` contribute no helper, no `insteadOf` rewrite, nothing.
  * `-C <neutral dir>` + `GIT_CEILING_DIRECTORIES` — the probe runs outside any repo,
    so no *repo-local* config applies either (the checkout's would otherwise).
  * `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS`/`SSH_ASKPASS` **unset** — git cannot ask
    a terminal for a credential, and cannot shell out to an inherited askpass helper.
  * `SSH_AUTH_SOCK` **unset** plus `IdentityAgent=none`, `IdentitiesOnly=yes`,
    `IdentityFile=/dev/null` — an agent key is a credential, and an ssh probe that
    silently used one is exactly the accident described above.
  * `BatchMode=yes` and `NumberOfPasswordPrompts=0` — no interactive fallback, so an
    unknown host key fails fast instead of hanging a TTY-less service.

None of layer 2 is applied to `authenticated_env()`, and that is not an oversight: the
authenticated probe and the push *must* reach this instance's deploy key, so scrubbing
the agent, the askpass channel, the identity options or the user's git config would
break the very thing they exist to exercise. They keep `GIT_TERMINAL_PROMPT=0` (a
TTY-less service must fail rather than hang) and `AUTH_SSH`'s `BatchMode=yes`.

Host-key checking is deliberately **not** weakened anywhere: an unknown host fails, and
that failure is *indeterminate*, never "private".

Which endpoint gets probed matters as much as the environment. Public readability is
exposed over https (and `git://`), never over ssh — an anonymous ssh probe is refused
for a public repo just as loudly as for a private one. So `anonymous_endpoints()`
derives the https endpoint from an ssh remote and probes **only** that.

The ssh URL is never an endpoint, and that is not a simplification — probing it is
actively wrong in both directions. ssh authenticates before it answers, so a read is
refused for a public repo exactly as loudly as for a private one (no signal); and the
probe runs on the owner's own box, where `~/.ssh/config` names the hub deploy key, so
the "anonymous" ssh read *succeeds* with the very credential the setup prescribes and
`decide` reads that success as PUBLIC. Observed on a real install: the documented
deploy-key setup reported its own private hub remote as PUBLICLY READABLE and refused
every push. Suppressing the config instead (`ssh -F /dev/null`) only moves the damage:
the alias then resolves to nothing, "could not resolve hostname" matches no
`DENIED_PATTERNS` entry, and the verdict becomes INDETERMINATE — refused forever again.

Deriving that twin from the documented remote, and why `ssh -G`
---------------------------------------------------------------
`deploy/README.md` has the owner give the hub remote an **ssh config alias** — a `Host`
block naming the deploy key — so the remote reads `buzai-hub:owner/hubs.git`. Two things
have to be right for that to work, and neither was:

  * git's scp-like `host:path` syntax is not a URL and has no `//`, so `urlsplit` reads
    `buzai-hub` as a *scheme*. Rejecting on "unknown scheme" before trying the scp-like
    form threw the documented remote away. The scp-like branch is therefore tried for
    anything that is not one of git's own transports.
  * a `Host` alias is not a hostname. `https://buzai-hub/owner/hubs.git` resolves
    nowhere, so even correct parsing yields a twin that can only ever be UNKNOWN.

So the host token is resolved through `ssh -G <host>` — ssh's own answer to "what does
this name mean after ~/.ssh/config", computed locally with no connection made — and the
twin is built from the resulting `HostName`. When that cannot be answered the endpoint
list is **empty**, never "just the ssh URL": an anonymous ssh probe is refused for a
public repo too, so accepting it alone would manufacture a PRIVATE verdict. An empty
list refuses, and the reason travels as a violation so `make hub-remote-check` names the
alias at setup time instead of leaving the owner with a backlog that never drains.

Three outcomes, never two
-------------------------
`decide()` is the pure core, and it has no permissive fallback:

  * **public** — any anonymous probe read the ref list. Refuse.
  * **private** — every anonymous probe was definitively refused **and** the
    authenticated probe succeeded. Only this permits a push. The authenticated probe is
    what makes "repository not found" mean *private* rather than *the URL is wrong or
    the host is down* — the same message from a stranger and from us means gone, from a
    stranger alone means hidden.
  * **indeterminate** — anything else: unreachable host, DNS failure, timeout, an error
    nobody recognized, or a remote with no derivable anonymous endpoint. Refuse and
    queue. "Assume private" is never reachable from here.

A fourth verdict, **local-only**, covers "no remote configured": a durability condition
(U6 warns about it), pointedly not the same thing as public.

The TTL cache
-------------
Verifying on every push would be a network round trip per write; caching by URL alone
would never notice a repository flipped to public through the web UI or by an org
policy change with the clone URL unchanged. So the verdict is cached with a TTL
(`CACHE_TTL_SECONDS`) and re-verified at service start, on expiry, and on URL change.
An expired cache means refuse-and-queue, never push. Only *private* verdicts are
cached, and a later non-private verdict evicts the entry.

"At service start" is a wire, not an aspiration: `deploy/claude-remote.service.template`
runs this module as an `ExecStartPre=-` line — `main()` -> `check(use_cache=False)`, a
fresh probe that rewrites or evicts the entry — ordered *before* the `ExecStartPost=`
pushes, so a restart can never push against a verdict nobody re-checked. The leading `-`
is what keeps a privacy answer from being able to block start: "do not push" and "do not
run the assistant" are different conclusions, and only the leak conditions in
`secrets_preflight` earn the second one.

The cache lives at `<hub>/.git/buzai/remote-verified.json` — durable, per-instance, and
inside the git directory so it is never committed and never dirties the hub working
tree (U2's `.buzai/remote-expected` marker is committed precisely because it *should*
travel with a restore; this must not). `write_cache` refuses to create a `.git`
directory that does not already exist, since a stray one turns a plain directory into a
broken repo.

Other notes
-----------
`hub_init.git()` is deliberately not reused for these calls: every invocation here is
network-facing or credential-sensitive and must be timeout-bounded and env-scrubbed,
which that signature does not express. Instead a single `GitRunner` callable is
injected — the `scripts/trust_check.py` `self_test_runner` pattern — so tests never
touch the network. `hub_init`'s marker parsing *is* reused, for the local-only report.

The authenticated probe sets `GIT_SSH_COMMAND`, which **overrides** `core.sshCommand`.
The hub deploy key must therefore be configured through `~/.ssh/config` (a `Host` alias
with `IdentityFile`), not through `core.sshCommand`. `deploy/README.md` documents it
that way, and that alias must carry a `HostName` line — see the derivation notes above.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hub_init import MARKER, parse_marker  # noqa: E402
from scripts.hub_paths import HubPathError, hub_dir  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
GIT = "git"
SSH = "ssh"

# One probe is a single TCP conversation; anything slower than this is a hung network,
# and a hung probe must never block the caller (git has no default network timeout).
PROBE_TIMEOUT_SECONDS = 20.0
# `ssh -G` parses config and connects to nothing, so this only has to cover a pathological
# include chain — but it runs on the push path, so it is bounded like everything else.
HOST_RESOLVE_TIMEOUT_SECONDS = 10.0
# Bounds how long a flip to public can go unnoticed. Cheap to re-verify (one ls-remote),
# and pushes are infrequent, so this is kept short rather than convenient.
CACHE_TTL_SECONDS = 3600.0

CACHE_PARTS = (".git", "buzai", "remote-verified.json")

# Probe outcomes.
READABLE = "readable"
DENIED = "denied"
UNKNOWN = "unknown"

# Verdicts.
PRIVATE = "private"
PUBLIC = "public"
INDETERMINATE = "indeterminate"
LOCAL_ONLY = "local-only"

# Substrings that make a *failed* probe definitively "not anonymously readable".
# Everything not listed here stays UNKNOWN, so the fail-closed direction is the default
# and a new git error message can only ever cost a refusal, never grant one.
#
# Every entry must be phrasing only git or a git host produces. A bare "not found" was
# one of these and is deliberately gone: any HTML error body — a proxy, a captive
# portal, a load balancer — that happens to contain those two words would otherwise be
# read as proof that the repository is private, which is the one direction this module
# must never guess in. "repository not found" cannot be reached that generically.
DENIED_PATTERNS = (
    "terminal prompts disabled",
    "could not read username",
    "could not read password",
    "authentication failed",
    "permission denied",
    "access denied",
    "repository not found",
    "returned error: 401",
    "returned error: 403",
    "returned error: 404",
    "401 unauthorized",
    "403 forbidden",
    "you must have read access",
    "invalid username or token",
    "requires authentication",
)

ANON_SSH = (
    "ssh -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none "
    "-o IdentityFile=/dev/null -o NumberOfPasswordPrompts=0 -o ConnectTimeout=10"
)
AUTH_SSH = "ssh -o BatchMode=yes -o ConnectTimeout=10"
LOCAL_ENV: dict[str, str | None] = {"GIT_TERMINAL_PROMPT": "0"}

# Environment channels NO network-facing git call may inherit: config injection that
# bypasses GIT_CONFIG_GLOBAL/SYSTEM entirely, and proxies, which decide who answers for
# the host. Each is *unset* rather than emptied — an empty `GIT_CONFIG_PARAMETERS` is
# parsed, an absent one is not.
SUPPRESSED_VARS = (
    "GIT_CONFIG",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_PARAMETERS",
    "GIT_PROXY_COMMAND",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
)

# `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` come in numbered pairs with no bound on
# `<n>`, so they are enumerated from the environment. Dropping `GIT_CONFIG_COUNT` alone
# would be enough for today's git; matching the pairs too means the suppression does not
# depend on that implementation detail staying true.
CONFIG_PAIR_VAR = re.compile(r"^GIT_CONFIG_(?:KEY|VALUE)_\d+$")

# The command-line half of the proxy suppression, shared by every network-facing call.
# `-c` on the command line outranks every config file, so this holds even where a proxy
# reached git through a channel the environment overlay does not know about.
NO_PROXY_ARGS = ("-c", "http.proxy=")

# `host:path`, git's scp-like remote syntax. The `(?!//)` keeps `scheme://…` out.
SCP_LIKE = re.compile(r"^(?:[^/@]+@)?(?P<host>[^/:]+):(?!//)(?P<path>.+)$")

# git's own transports. Anything whose `urlsplit` scheme is NOT one of these is a
# candidate for the scp-like form, because `buzai-hub:owner/hubs.git` — the alias form
# `deploy/README.md` tells the owner to use — parses as scheme `buzai-hub`. The list
# matters in the other direction too: `file:/srv/hubs` would otherwise match SCP_LIKE
# with host `file`, and yield a probe URL for a host that does not exist.
TRANSPORT_SCHEMES = frozenset({"http", "https", "git", "ssh", "ftp", "ftps", "file"})


@dataclass(frozen=True)
class GitResult:
    """One git invocation's outcome. A nonzero returncode is a value, not an exception."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


# (argv after the git binary, env overlay where None means *unset*, timeout seconds)
GitRunner = Callable[[Sequence[str], Mapping[str, str | None], float], GitResult]

# A remote's ssh host token -> the real hostname it means, or None when that cannot be
# answered. Injected so no test ever reads the real `~/.ssh/config`.
HostResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class CacheEntry:
    """A remembered verdict: which URL, what was decided, and when."""

    url: str
    verdict: str
    verified_at: datetime


@dataclass(frozen=True)
class AnonymousEndpoints:
    """Where to probe as a stranger, and — when that list is empty — why it is.

    `problem` is the owner-facing sentence for a remote whose anonymous endpoint cannot
    be derived at all. It is carried rather than swallowed because the alternative is a
    verdict of INDETERMINATE with no cause named, which reads as a transient network
    blip while actually being permanent: every push refused, forever.
    """

    urls: tuple[str, ...] = ()
    problem: str | None = None


@dataclass(frozen=True)
class Verification:
    """The answer to "may hub history be pushed to this remote right now?"."""

    verdict: str
    remote: str | None
    url: str | None  # always redacted — safe to print into the journal
    detail: str
    source: str  # "probe" | "cache"
    # Conditions the owner must fix before any push can happen: a credential embedded in
    # the remote URL, or a remote whose anonymous endpoint cannot be derived. Each is
    # reported by name so it is fixed at `make hub-remote-check` rather than discovered
    # as a backlog that never drains.
    violations: tuple[str, ...] = ()

    @property
    def push_allowed(self) -> bool:
        """Only a proven-private remote with no outstanding violation may be pushed to."""
        return self.verdict == PRIVATE and not self.violations


# --- git invocation ---------------------------------------------------------------


def default_git_runner(
    args: Sequence[str],
    env: Mapping[str, str | None] | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    git_bin: str = GIT,
) -> GitResult:
    """Run git with an env overlay and a hard timeout. Never raises.

    A timeout or an unrunnable binary comes back as a failed `GitResult`, which
    classifies as UNKNOWN and therefore refuses — the fail-closed direction.
    """
    child = os.environ.copy()
    for key, value in (env or {}).items():
        if value is None:
            child.pop(key, None)
        else:
            child[key] = value
    try:
        completed = subprocess.run(
            [git_bin, *args], capture_output=True, text=True, timeout=timeout, env=child
        )
    except subprocess.TimeoutExpired:
        return GitResult(124, "", f"timed out after {timeout}s", timed_out=True)
    except OSError as e:
        return GitResult(127, "", f"cannot run {git_bin!r}: {e}")
    return GitResult(completed.returncode, completed.stdout, completed.stderr)


# --- URLs -------------------------------------------------------------------------


def redact(url: str) -> str:
    """Drop any embedded userinfo. Pure.

    Every printed URL goes through this: the unit sends stderr to the journal, and a
    URL-embedded token would otherwise be logged in plaintext.
    """
    split = urlsplit(url)
    if not split.hostname:
        return url
    netloc = split.hostname if not split.port else f"{split.hostname}:{split.port}"
    return urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))


def url_credential_problem(url: str) -> str | None:
    """Why this remote URL must not be used, or None. Pure.

    A token embedded in the remote URL is forbidden outright: git echoes the remote on
    failure and the service sends stderr to the journal, so it would be written to logs
    in plaintext on exactly the push failures this design expects routinely.
    """
    split = urlsplit(url)
    if split.password or (split.username and split.scheme in ("http", "https")):
        return (
            f"the hub remote URL embeds a credential ({redact(url)}) — git echoes the "
            "remote on failure and the service logs stderr, so it would land in the "
            "journal in plaintext; use an ssh deploy key scoped to the hub repo instead"
        )
    return None


def default_host_resolver(
    host: str, timeout: float = HOST_RESOLVE_TIMEOUT_SECONDS, ssh_bin: str = SSH
) -> str | None:
    """The real hostname `host` means after `~/.ssh/config`, or None. Never raises.

    `ssh -G <host>` is ssh's own answer: it applies the config exactly as a connection
    would — `Host` blocks, `Match`, `Include` — and prints the effective settings without
    contacting anything. A `HostName` line is what a `Host` alias exists to supply; a
    name with no block of its own comes back as itself, which is the right answer too.

    None means "ssh could not say" — no ssh binary, a config it refuses to parse, or a
    hang. The caller must treat that as *unverifiable*, never as "the alias is fine".
    """
    try:
        completed = subprocess.run(
            [ssh_bin, "-G", host], capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        if key.lower() == "hostname":
            return value.strip() or None
    return None


def _ssh_endpoints(host: str, path: str, resolve_host: HostResolver | None) -> AnonymousEndpoints:
    """The https twin of an ssh remote — and only the twin.

    The twin is what can prove the repo public, so failing to derive it must produce an
    empty list and a named reason — not the ssh URL on its own, which is refused for
    public and private repos alike and would therefore read as proof of privacy.

    The ssh URL is not returned *alongside* it either, for the mirror-image reason: on
    the owner's own box that read is not anonymous. `~/.ssh/config` supplies the deploy
    key `deploy/README.md` tells them to create, the read succeeds, and a success is
    proof of PUBLIC. See the module docstring.
    """
    hostname = resolve_host(host) if resolve_host is not None else host
    if not hostname:
        return AnonymousEndpoints(
            (),
            f"the hub remote's ssh host {host!r} could not be resolved to a real hostname "
            f"(`ssh -G {host}` gave no usable HostName), so the anonymous https endpoint "
            f"cannot be derived and every push will be refused; give it a `Host {host}` "
            f"block with a `HostName` line in ~/.ssh/config (see deploy/README.md), or "
            f"point the remote at the provider's real ssh URL",
        )
    return AnonymousEndpoints((f"https://{hostname}/{path.lstrip('/')}",))


def anonymous_endpoints(url: str, resolve_host: HostResolver | None = None) -> AnonymousEndpoints:
    """Endpoints to probe as a stranger. Pure given `resolve_host`.

    Public readability is served over https / `git://`, never over ssh, so an ssh remote
    contributes its https twin and nothing else — see `_ssh_endpoints` for why probing
    the ssh URL too is not merely useless but wrong.
    An empty list is not "safe", it is unverifiable — `decide` turns it into a refusal.

    `resolve_host` is None for the parsing-only view (the host token is taken at face
    value); `verify` passes the real resolver. See the module docstring for why an ssh
    config alias makes that resolution load-bearing rather than cosmetic.
    """
    raw = url.strip()
    if not raw:
        return AnonymousEndpoints()
    split = urlsplit(raw)
    scheme = split.scheme.lower()
    if scheme in ("http", "https", "git"):
        return AnonymousEndpoints((redact(raw),))
    if scheme == "ssh":
        if not split.hostname or not split.path:
            return AnonymousEndpoints()
        return _ssh_endpoints(split.hostname, split.path, resolve_host)
    if scheme in TRANSPORT_SCHEMES:
        return AnonymousEndpoints()  # file://, ftp:// — no anonymous endpoint to derive
    # Not one of git's transports, so `scheme` may well be an ssh config alias: this is
    # git's scp-like `host:path`, which has no `//` and is not a URL at all.
    match = SCP_LIKE.match(raw)
    if match:
        return _ssh_endpoints(match.group("host"), match.group("path"), resolve_host)
    return AnonymousEndpoints()  # a bare local path


# --- probing and the decision ------------------------------------------------------


def no_config_injection(environ: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """Layer 1: the overlay EVERY network-facing git call needs. See the module docstring.

    Strips the git-config injection channels and the proxy channels, and nothing else —
    it is the floor, not the whole story, and it is safe for calls that must
    authenticate because it removes no credential of theirs.

    Pure given `environ` (the parent environment, `os.environ` by default), which is
    read only to *enumerate* what must be unset: the `GIT_CONFIG_KEY_<n>`/`_VALUE_<n>`
    pairs are numbered, so the names cannot be written down in advance. A `None` value
    means unset in the child, which is what `default_git_runner` does.
    """
    source = os.environ if environ is None else environ
    env: dict[str, str | None] = dict.fromkeys(SUPPRESSED_VARS)
    for name in source:
        if CONFIG_PAIR_VAR.match(name):
            env[name] = None
    return env


def authenticated_env(environ: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """The overlay for git calls that MUST authenticate: the auth probe and the pushes.

    Layer 1 plus the two settings a TTY-less service needs, and deliberately **none** of
    `anonymous_env`'s credential suppression: these calls exist to exercise this
    instance's deploy key, so scrubbing the ssh agent, askpass, the identity options or
    the user's git config would break them rather than harden them.
    """
    return {
        **no_config_injection(environ),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": AUTH_SSH,
    }


def anonymous_env(
    neutral_dir: str, environ: Mapping[str, str] | None = None
) -> dict[str, str | None]:
    """Layer 1 + layer 2: the credential-suppressing overlay, anonymous probe only.

    Everything `no_config_injection` removes, plus every channel git could obtain a
    credential through. See the module docstring for why the authenticated calls must
    not get this half.
    """
    return {
        **no_config_injection(environ),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": None,
        "SSH_ASKPASS": None,
        "SSH_ASKPASS_REQUIRE": "never",
        "SSH_AUTH_SOCK": None,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CEILING_DIRECTORIES": neutral_dir,
        "GIT_SSH_COMMAND": ANON_SSH,
    }


def anonymous_probe(url: str, runner: GitRunner, timeout: float) -> GitResult:
    """Read `url`'s ref list as a stranger would. Success means the repo is public.

    The `-c` resets are belt to the environment's braces: a helper or a proxy that
    reached git through a channel the overlay does not know about is still cleared here,
    and command-line `-c` outranks every config file.
    """
    neutral = tempfile.gettempdir()
    args = ["-C", neutral, "-c", "credential.helper=", *NO_PROXY_ARGS, "ls-remote", url, "HEAD"]
    return runner(args, anonymous_env(neutral), timeout)


def authenticated_probe(hub: Path, remote: str, runner: GitRunner, timeout: float) -> GitResult:
    """Read the remote's ref list *with* this instance's credential.

    Success is what separates "private" from "misconfigured or unreachable": it proves
    the repository exists, the host answers, and the deploy key works. Which is exactly
    why it is injection-suppressed too — an `insteadOf` rewrite pointing this at a host
    that answers manufactures the one verdict that permits a push.
    """
    args = ["-C", str(hub), *NO_PROXY_ARGS, "ls-remote", remote, "HEAD"]
    return runner(args, authenticated_env(), timeout)


def classify(result: GitResult) -> str:
    """One probe -> READABLE / DENIED / UNKNOWN. Pure.

    Only an explicitly recognized refusal counts as DENIED; silence, a timeout and any
    unfamiliar error are UNKNOWN, so an unrecognized message can never be mistaken for
    proof of privacy.
    """
    if result.timed_out:
        return UNKNOWN
    if result.returncode == 0:
        return READABLE
    text = f"{result.stderr}\n{result.stdout}".lower()
    return DENIED if any(p in text for p in DENIED_PATTERNS) else UNKNOWN


def decide(anon: Sequence[tuple[str, str]], auth: str) -> tuple[str, str]:
    """(verdict, reason) from the probe outcomes. Pure, and the control everything rests on.

    `anon` is [(url, outcome)]. There is no permissive fallback: PRIVATE is the last,
    fully-constrained branch, and every other path lands on PUBLIC or INDETERMINATE.
    """
    readable = [url for url, outcome in anon if outcome == READABLE]
    if readable:
        return PUBLIC, f"an anonymous read succeeded at {', '.join(readable)} — the repo is public"
    if not anon:
        return INDETERMINATE, "no anonymous endpoint could be derived from the remote URL"
    if auth != READABLE:
        return INDETERMINATE, (
            "the authenticated probe did not succeed either, so the remote may be "
            "unreachable rather than private"
        )
    inconclusive = [url for url, outcome in anon if outcome != DENIED]
    if inconclusive:
        return INDETERMINATE, f"the anonymous probe was inconclusive for {', '.join(inconclusive)}"
    return PRIVATE, "every anonymous probe was refused while the authenticated probe succeeded"


# --- remotes ----------------------------------------------------------------------


def hub_remote(hub: Path, runner: GitRunner, timeout: float) -> tuple[str | None, str | None]:
    """(remote name, URL) for the hub repo. (None, None) means local-only.

    A *failed* listing is not local-only — it means the directory is not a usable repo,
    which the caller must not mistake for "configured but no remote". It comes back as
    ("", None) so `verify` reports it as indeterminate.
    """
    listed = runner(["-C", str(hub), "remote"], LOCAL_ENV, timeout)
    if listed.returncode != 0:
        return "", None
    names = listed.stdout.split()
    if not names:
        return None, None
    name = "origin" if "origin" in names else names[0]
    got = runner(["-C", str(hub), "remote", "get-url", name], LOCAL_ENV, timeout)
    url = got.stdout.strip() if got.returncode == 0 else ""
    return name, url or None


def config_url(url: str) -> str:
    """A remote URL in a form `git config --get-urlmatch` accepts. Pure.

    git's own scp-like `host:path` syntax is not a URL, and urlmatch rejects it outright
    (`fatal: invalid URL scheme name`, exit 128). buzai's public origin is written that
    way, so without this normalization the credential-helper check below would have
    exited 128, been read as "nothing found", and become a silent no-op on the exact
    repository it exists to guard.
    """
    if urlsplit(url).scheme:
        return url
    match = SCP_LIKE.match(url.strip())
    if match:
        return f"ssh://{match.group('host')}/{match.group('path').lstrip('/')}"
    return url


def helper_shape(helper: str) -> str:
    """A credential helper's SHAPE, safe to print. Pure. Never its configured value.

    Git accepts an *inline shell* helper — `credential.helper = !f() { echo
    password=$MY_TOKEN; }; f` — which is a documented idiom and can legitimately contain a
    token verbatim. The finding below is printed to stderr and the unit sends stderr to
    the journal, so echoing the configured value would write a credential to the logs in
    plaintext: the check meant to protect the public origin would itself become the leak.

    The first whitespace-delimited token identifies the helper for every remedy the
    message names (`store`, `cache`, `osxkeychain`, a path to a binary) and carries no
    secret; arguments after it might (`--token=…`), so they are dropped. Anything starting
    with `!` is a shell fragment whose first token is meaningless anyway, and it is
    replaced wholesale rather than trimmed — there is no prefix of a shell program that is
    known not to contain a secret.
    """
    text = helper.strip()
    if text.startswith("!"):
        return "<inline shell helper>"
    return text.split()[0] if text.split() else "<empty>"


def origin_credential_violations(
    repo_root: Path, runner: GitRunner, timeout: float = PROBE_TIMEOUT_SECONDS
) -> list[str]:
    """Credential helpers that would let this instance push to the PUBLIC repo.

    The structural barrier is that the public checkout cannot push. This plan could
    break that (a credential helper installed for the hub remote's host applies to the
    public origin too), so it is checked rather than assumed. `--get-urlmatch` resolves
    the *effective* helper for the origin URL across system, global and local config,
    including `[credential "https://host"]` sections.

    `git config` exits 1 for "no such key" and nonzero-other for "could not evaluate";
    the two are kept apart deliberately, because collapsing them is how a safety check
    turns into a silently-passing no-op.
    """
    got = runner(["-C", str(repo_root), "config", "--get", "remote.origin.url"], LOCAL_ENV, timeout)
    origin = got.stdout.strip() if got.returncode == 0 else ""
    where = f"the public origin ({redact(origin)})" if origin else "this checkout"

    found = None
    if origin:
        found = runner(
            [
                "-C",
                str(repo_root),
                "config",
                "--get-urlmatch",
                "credential.helper",
                config_url(origin),
            ],
            LOCAL_ENV,
            timeout,
        )
    if found is None or found.returncode not in (0, 1):
        # No origin, or urlmatch could not evaluate this URL. Fall back to the unscoped
        # helper list rather than reporting clean — but note that URL-scoped helpers are
        # then invisible, so a hard failure here is reported, never swallowed.
        found = runner(
            ["-C", str(repo_root), "config", "--get-all", "credential.helper"], LOCAL_ENV, timeout
        )
        if found.returncode not in (0, 1):
            detail = found.stderr.strip() or f"git config exited {found.returncode}"
            return [
                f"cannot determine whether a credential helper is configured for {where} "
                f"— treated as unverified rather than clean: {detail}"
            ]
    # An empty value is git's idiom for *clearing* the helper list, not for adding one.
    # Only the helper's SHAPE is reported — see `helper_shape`: an inline shell helper can
    # carry a token, and this message goes to the journal.
    helpers = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    return [
        f"a git credential helper ({helper_shape(helper)}) is configured for {where} — this "
        f"instance must have no way to push to the public repo; remove it with "
        f"`git -C {repo_root} config --unset-all credential.helper`"
        for helper in helpers
    ]


# --- the TTL cache ----------------------------------------------------------------


def default_cache_path(hub: Path) -> Path:
    """Where this hub's verdict is remembered. Inside `.git/`, so it is never committed."""
    return hub.joinpath(*CACHE_PARTS)


def read_cache(path: Path) -> CacheEntry | None:
    """The remembered verdict, or None when there is not a well-formed one.

    Every failure mode — missing, corrupt, half-written, naive timestamp — reads as
    "no entry", which forces a fresh probe. None of them can read as "verified".
    """
    try:
        data = json.loads(path.read_text())
        verified_at = datetime.fromisoformat(data["verified_at"])
        if verified_at.tzinfo is None:
            return None
        return CacheEntry(str(data["url"]), str(data["verdict"]), verified_at)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def write_cache(path: Path, entry: CacheEntry) -> bool:
    """Record a verdict atomically. False when it could not be written (never fatal).

    Refuses to create the `.git` directory itself: a stray `.git` would turn a plain
    directory into a repo git then reports as broken.

    The temp name carries the **pid**, which is what makes the write atomic *between
    sessions* and not merely against a reader. Two Claude sessions in the same directory
    verify the same hub concurrently — `--spawn=same-dir` is what the unit runs — and with
    one fixed temp name the second writer overwrites the first's temp file before the
    first has replaced: the first's `replace()` then fails on a file that is gone, so it
    reports success-by-return-value while the cache holds the *other* session's entry.
    A lost or torn verdict is not cosmetic here; it is the record of whether a push is
    permitted.

    This mirrors `hub_commit.atomic_write` by construction rather than by import:
    `hub_commit` imports this module, so importing it back is a cycle that fails at
    interpreter start. The two must stay in step.
    """
    if not path.parent.parent.is_dir():
        return False
    payload = {
        "url": entry.url,
        "verdict": entry.verdict,
        "verified_at": entry.verified_at.isoformat(timespec="seconds"),
    }
    temp = path.with_name(f"{path.name}.buzai-tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(payload, indent=2) + "\n")
        temp.replace(path)
    except OSError:
        temp.unlink(missing_ok=True)
        return False
    return True


def clear_cache(path: Path) -> None:
    """Evict a verdict that no longer holds — a stale `private` must never survive."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cache_is_usable(entry: CacheEntry | None, url: str, now: datetime, ttl_seconds: float) -> bool:
    """Whether a cached verdict may stand in for a fresh probe. Pure.

    Only a *private* verdict, for *this* URL, within the TTL. A negative age (the clock
    moved backwards) is treated as expired rather than as an unbounded window.
    """
    if entry is None or entry.verdict != PRIVATE or entry.url != url:
        return False
    age = (now - entry.verified_at).total_seconds()
    return 0 <= age < ttl_seconds


# --- verification -----------------------------------------------------------------


def verify(
    hub: Path,
    *,
    runner: GitRunner,
    now: datetime,
    cache_path: Path | None = None,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    use_cache: bool = True,
    resolve_host: HostResolver = default_host_resolver,
) -> Verification:
    """May hub history be pushed to `hub`'s remote?

    `use_cache=False` forces a fresh probe — what service start and `make hub-remote-check`
    want. The push path leaves it True so a burst of writes costs one verification.

    `resolve_host` is the seam for `~/.ssh/config` alias resolution, injected so tests
    never read the real file.
    """
    name, raw_url = hub_remote(hub, runner, timeout)
    if name is None:
        return Verification(
            LOCAL_ONLY, None, None, "no remote is configured — history stays on this box", "probe"
        )
    if raw_url is None:
        detail = (
            f"cannot read the URL of remote {name!r}"
            if name
            else f"cannot read remotes from {hub} — is it a git repository?"
        )
        return Verification(INDETERMINATE, name or None, None, detail, "probe")

    url = redact(raw_url)
    violations = tuple(v for v in (url_credential_problem(raw_url),) if v)
    path = cache_path or default_cache_path(hub)
    entry = read_cache(path)
    if use_cache and cache_is_usable(entry, url, now, ttl_seconds) and entry is not None:
        detail = f"{url} verified private at {entry.verified_at.isoformat(timespec='seconds')}"
        return Verification(PRIVATE, name, url, detail, "cache", violations)

    endpoints = anonymous_endpoints(raw_url, resolve_host)
    violations += tuple(v for v in (endpoints.problem,) if v)
    anon = [(u, classify(anonymous_probe(u, runner, timeout))) for u in endpoints.urls]
    auth = classify(authenticated_probe(hub, name, runner, timeout))
    verdict, reason = decide(anon, auth)
    if verdict == PRIVATE:
        write_cache(path, CacheEntry(url, PRIVATE, now))
    else:
        clear_cache(path)
    return Verification(verdict, name, url, f"{url}: {reason}", "probe", violations)


# --- the verb ---------------------------------------------------------------------


def local_only_note(hub: Path) -> str:
    """The extra line the local-only report carries: how long nothing has been off-box."""
    try:
        created = parse_marker((hub / MARKER).read_text())
    except (OSError, ValueError):
        return ""
    return f" (store created {created.isoformat(timespec='seconds')})"


def check(
    hub: Path,
    repo_root: Path,
    *,
    runner: GitRunner,
    now: datetime,
    use_cache: bool = False,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    resolve_host: HostResolver = default_host_resolver,
) -> int:
    """Report the verdict; 1 when a push must be refused for a PRIVACY reason.

    Exit 0 does **not** mean "push now" — local-only exits 0 because it is a durability
    condition (U6 warns; making it fatal would convert degraded knowledge into an
    outage). Programmatic callers use `verify(...).push_allowed`, never the exit code.
    """
    result = verify(
        hub,
        runner=runner,
        now=now,
        ttl_seconds=ttl_seconds,
        timeout=timeout,
        use_cache=use_cache,
        resolve_host=resolve_host,
    )
    origin = origin_credential_violations(repo_root, runner, timeout)
    failed = False

    if result.verdict == LOCAL_ONLY:
        print(
            f"hub-remote: {hub} has no remote — history is local-only and nothing has "
            f"been pushed off-box{local_only_note(hub)}"
        )
        print("  run `make hub-init` for the owner-run commands that attach a private remote")
    elif result.verdict == PRIVATE:
        print(f"hub-remote: remote {result.remote!r} verified PRIVATE — {result.detail}")
        print(f"  source: {result.source}; re-verified at least every {ttl_seconds:.0f}s")
    elif result.verdict == PUBLIC:
        print(
            f"hub-remote FAIL: remote {result.remote!r} is PUBLICLY READABLE — {result.detail}; "
            "push refused",
            file=sys.stderr,
        )
        failed = True
    else:
        print(
            f"hub-remote FAIL: remote {result.remote!r} could not be confirmed private — "
            f"{result.detail}; push refused and queued (never assumed private)",
            file=sys.stderr,
        )
        failed = True

    for violation in (*result.violations, *origin):
        print(f"hub-remote FAIL: {violation}", file=sys.stderr)
        failed = True
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub_remote.py",
        description="Prove the hub remote is PRIVATE (anonymous readability probe) and exit.",
    )
    parser.add_argument(
        "--if-initialized",
        action="store_true",
        help=(
            "for the service unit ONLY: a hub store that does not exist yet is reported as "
            "a note and exits 0. Without it a missing store fails, so `make "
            "hub-remote-check` never reports a store that is not there as verified"
        ),
    )
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = hub_dir()
    except HubPathError as e:
        print(f"hub-remote FAIL: {e}", file=sys.stderr)
        return 1
    print(f"hub-remote: hubs resolve to {location}")
    if not (location.path / ".git").exists():
        # Every instance between `make setup` and `make hub-init` is here, and this module
        # is the unit's ExecStartPre, so a FAIL at each start would spend the word the
        # journal reserves for something actually wrong on a routine state. That downgrade
        # belongs to the unit alone, via `--if-initialized`, and is the NOTE tier of
        # `secrets_preflight.report` (untagged, as it prints "no hub store on this
        # instance"): its three tiers are note for a legitimate empty state, WARN for a
        # durability condition, FAIL for a leak or something that could not be done. Run
        # by hand as `make hub-remote-check`, the owner asked for a proof, and "there is
        # no store to prove anything about" is a failure to give one.
        if args.if_initialized:
            print(
                f"hub-remote: {location.path} is not a hub repo yet — nothing to verify "
                "(run `make hub-init`)",
                file=sys.stderr,
            )
            return 0
        print(
            f"hub-remote FAIL: {location.path} is not a hub repo yet — run `make hub-init`",
            file=sys.stderr,
        )
        return 1
    return check(location.path, REPO_ROOT, runner=default_git_runner, now=datetime.now(UTC))


if __name__ == "__main__":
    raise SystemExit(main())
