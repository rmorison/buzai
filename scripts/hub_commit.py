"""Record knowledge into the private hub repo: one operation, one reviewable commit.

This is the single writer. Everything that adds to, corrects, or removes hub content
goes through `record()`, which turns one *operation* into one commit whose message can
be reviewed without the diff (R5), attributed to a distinct assistant identity with an
`instruction-source` trailer (R6), and pushed off-box when — and only when — the remote
has been proven private (R7, via `scripts/hub_remote.verify`).

Why an operation and not file content
-------------------------------------
`record()` takes append-entry / replace-section / remove-entry, **never** pre-rendered
file text. If the caller rendered the whole file, the read happened outside the
critical section and two sessions attached to the same hub silently clobber each other:
both produce commits, one update vanishes, and the losing commit's message describes a
fact the file no longer contains — which breaks the premise of "reviewable without the
diff". The read must happen where the lock already is. The concurrency test asserts on
the final *file content* for exactly this reason; a commit-count assertion passes
against the broken version.

What the lock covers, and what it deliberately does not
-------------------------------------------------------
    [lock] read -> modify -> atomic write -> credential scan -> commit [unlock] -> push

The push runs **after** the lock is released, bounded by an explicit timeout. git has
no default network timeout, so a blackholed connection inside the lock would hold it
indefinitely and block every write on the box — inverting R14, which says an unreachable
remote must never block recording. Push is idempotent and its failure is already handled
by the backlog, so excluding it costs nothing.

The lock is `fcntl.flock` on a file under the hub repo's `.git/` (never the worktree, so
it can never be committed or dirty the tree). flock is released by the kernel when the
holding process dies, so an OOM-killed session cannot wedge the box — the next writer
acquires immediately and reports the stale pid record it cleared. A *live* holder makes
the next writer queue, bounded by `LOCK_TIMEOUT_SECONDS`.

The credential scan
-------------------
`scripts/publish_gate.py` is the wrong tool twice over: on a deployment its pattern file
is absent so it matches almost nothing, and on a workstation its private patterns
enumerate the hostnames and handles a legitimate personal hub is *supposed* to contain,
so it would refuse valid commits. This scanner therefore looks for credential **shapes**
only — private-key headers, known token prefixes, `Authorization:` values, secret-shaped
assignments, and high-entropy opaque strings — and never for PII. It runs over the lines
this operation *added*, so pre-existing content cannot make every future write refuse.
A match rolls the file back to its prior content and reports in-turn: the commit is the
durability boundary, so a refusal must leave nothing behind on disk either.

Reconciling a dirty tree
------------------------
Content already sitting uncommitted in the hub worktree at entry is committed first,
under a distinct `buzai unattributed` identity with an `instruction-source: unattributed`
trailer. Left alone it would either stay invisible to review forever or get swept into
the assistant's commit under the assistant's name — both worse than an honest "this was
found on disk and nobody claims it". A dirty file whose content trips the credential
scan is left dirty and reported rather than committed; the assistant's own write still
proceeds, because the commit is path-scoped — *unless* the flagged file is the very file
this operation targets. Path-scoping protects the other files, not that one: the commit
names the target path, so it would sweep the flagged pre-existing content in with the
new entry, and the added-lines scan cannot catch it (those lines were already on disk, so
they are not "added"). That case is refused outright, before anything is written.

Who may be named as the instruction source
------------------------------------------
`record()` accepts all of `SOURCES`; the command line accepts only `CLI_SOURCES`.
`owner-correction` is API-only because `hub_review` excludes that source from the review
queue — it is meaningful solely as the output of the rejection flow, which also writes
the review note that accounts for it. See `CLI_SOURCES`.

Push, backlog, and divergence
-----------------------------
Push failure is never fatal: the commit stands and the sha is recorded in a backlog file
under `.git/` (count + oldest age, which U5/U6 surface). Retry happens on the next write
and via `--retry-push` at service start. Divergence — the remote holding commits this
repo does not — **halts and surfaces**. It never rebases, merges, or force-pushes:
pulling remote commits into files the assistant reads back as trusted source of truth is
a genuine inbound write path, and force-pushing would discard the owner's work.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hub_paths import HubPathError, hub_dir  # noqa: E402
from scripts.hub_remote import (  # noqa: E402
    LOCAL_ENV,
    LOCAL_ONLY,
    NO_PROXY_ARGS,
    GitResult,
    GitRunner,
    Verification,
    authenticated_env,
    default_git_runner,
)
from scripts.hub_remote import verify as verify_remote  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# Operations. The whole public vocabulary of "what may change in a hub".
APPEND_ENTRY = "append-entry"
REPLACE_SECTION = "replace-section"
REMOVE_ENTRY = "remove-entry"
OPERATIONS = (APPEND_ENTRY, REPLACE_SECTION, REMOVE_ENTRY)

# Instruction sources (R6). `unattributed` is not selectable by a caller — it is what
# reconciliation stamps on content found uncommitted, whose author is genuinely unknown.
AUTONOMOUS = "autonomous"
OWNER_DIRECTED = "owner-directed"
OWNER_CORRECTION = "owner-correction"
UNATTRIBUTED = "unattributed"
SOURCES = (AUTONOMOUS, OWNER_DIRECTED, OWNER_CORRECTION)

# What the *command line* may ask for, which is deliberately narrower than `SOURCES`.
# `owner-correction` is excluded: `hub_review.reviewable()` treats that source as "this
# commit IS the owner's verdict" and keeps it out of the review queue forever, which is
# only sound when the value is produced by the rejection flow that also writes the
# matching review note. Reached from the CLI it would mint hub content that no owner can
# ever be shown — the one outcome the whole review loop exists to prevent. Restricting
# the parser rather than the API is what keeps `hub_review._correct()` working unchanged
# while making the accident unreachable.
CLI_SOURCES = (AUTONOMOUS, OWNER_DIRECTED)

# Write outcomes.
COMMITTED = "committed"
REFUSED = "refused"
FAILED = "failed"

# Push outcomes.
PUSHED = "pushed"
PUSH_REFUSED = "push-refused"
PUSH_FAILED = "push-failed"
DIVERGED = "diverged"
PUSH_LOCAL_ONLY = "local-only"
PUSH_SKIPPED = "not-attempted"
NOTHING_TO_PUSH = "nothing-to-push"

# A distinct identity, set per invocation through the environment rather than written
# into the repo's config: `git log --author` separates assistant writes from the owner's
# without this script ever mutating global git state.
ASSISTANT_IDENTITY = ("buzai assistant", "assistant@buzai.invalid")
UNATTRIBUTED_IDENTITY = ("buzai unattributed", "unattributed@buzai.invalid")

TRAILER_SOURCE = "instruction-source"
TRAILER_FILE = "hub-file"
TRAILER_OPERATION = "hub-operation"

LOCK_PARTS = (".git", "buzai", "write.lock")
BACKLOG_PARTS = (".git", "buzai", "push-backlog.json")

# A local git call is milliseconds; anything near this is a wedged repo, not slow disk.
GIT_TIMEOUT_SECONDS = 30.0
# Long enough to queue behind a real write (read + commit), short enough that a session
# reports a problem instead of appearing to hang.
LOCK_TIMEOUT_SECONDS = 30.0
# The one network call. git has no default network timeout, which is the whole reason
# the push sits outside the lock.
PUSH_TIMEOUT_SECONDS = 60.0

# The push environment is `hub_remote.authenticated_env()`, called per invocation rather
# than frozen into a constant here: it enumerates the numbered `GIT_CONFIG_KEY_<n>` pairs
# out of the *current* environment, and a constant built at import time would miss any
# that appeared later — and would read as suppression while suppressing nothing.

# Substrings that mean "the remote has commits this repo does not". Recognized so the
# operation can halt loudly rather than tempting anyone into a rebase or a --force.
DIVERGENCE_PATTERNS = (
    "non-fast-forward",
    "fetch first",
    "updates were rejected",
    "! [rejected]",
    "behind its remote counterpart",
)


class HubCommitError(Exception):
    """The write could not be completed. Never reported to the caller as success."""


class LockBusy(HubCommitError):
    """Another writer held the lock for longer than the acquisition timeout."""


# --- the operation ----------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """What is to change in one hub file. `section` scopes it to one heading.

    `text` is an entry (append/remove) or a section body (replace). It is never a whole
    file: see the module docstring for why that distinction is the unit's spine.
    """

    kind: str
    file: str
    text: str = ""
    section: str | None = None

    def describe(self) -> str:
        """One clause stating what happened, for the commit message body (R5)."""
        where = f" under {self.section!r}" if self.section else ""
        lines = len([line for line in self.text.splitlines() if line.strip()])
        if self.kind == APPEND_ENTRY:
            return f"appended a {lines}-line entry to {self.file}{where}"
        if self.kind == REPLACE_SECTION:
            return f"replaced the {self.section!r} section of {self.file} with {lines} line(s)"
        return f"removed a {lines}-line entry from {self.file}{where}"


@dataclass(frozen=True)
class WriteRequest:
    """One operation plus the review-facing story: what changed, why, and who asked."""

    operation: Operation
    summary: str
    reason: str
    source: str = AUTONOMOUS


@dataclass(frozen=True)
class Finding:
    """A credential-shaped match. Carries the rule and line only — never the value."""

    rule: str
    line: int

    def __str__(self) -> str:
        return f"line {self.line}: {self.rule}"


@dataclass(frozen=True)
class Backlog:
    """Commits that exist locally but have not reached the remote."""

    pending: tuple[tuple[str, datetime], ...] = ()
    last_error: str = ""
    last_attempt: datetime | None = None

    @property
    def count(self) -> int:
        return len(self.pending)

    @property
    def oldest(self) -> datetime | None:
        return min((at for _, at in self.pending), default=None)

    def oldest_age_seconds(self, now: datetime) -> float | None:
        """How long the oldest unpushed commit has been stuck. None when empty."""
        oldest = self.oldest
        return None if oldest is None else max(0.0, (now - oldest).total_seconds())


@dataclass(frozen=True)
class PushOutcome:
    """What the (post-lock, timeout-bounded) push attempt did."""

    status: str
    detail: str
    backlog: Backlog = field(default_factory=Backlog)

    @property
    def ok(self) -> bool:
        return self.status in (PUSHED, NOTHING_TO_PUSH)


@dataclass(frozen=True)
class Reconciliation:
    """Uncommitted content found at entry and what became of it."""

    commit: str | None = None
    committed: tuple[str, ...] = ()
    skipped: tuple[tuple[str, tuple[Finding, ...]], ...] = ()

    def blocking(self, file: str) -> tuple[tuple[str, tuple[Finding, ...]], ...]:
        """The skipped entries that cover `file` — the ones a write to it must not pass.

        An untracked *directory* is one porcelain record (`sub/`), so a file inside it is
        covered by that record even though the strings differ.
        """
        return tuple((p, f) for p, f in self.skipped if covers(p, file))


def covers(path: str, file: str) -> bool:
    """Whether a working-tree path names `file`, directly or as its directory. Pure."""
    return file == path or file.startswith(path.rstrip("/") + "/")


@dataclass(frozen=True)
class WriteResult:
    """The whole outcome. `status` is the durability answer; `push` is best-effort."""

    status: str
    commit: str | None
    file: str
    detail: str
    push: PushOutcome = field(default_factory=lambda: PushOutcome(PUSH_SKIPPED, "not attempted"))
    findings: tuple[Finding, ...] = ()
    reconciled: Reconciliation = field(default_factory=Reconciliation)
    lock_note: str = ""

    @property
    def recorded(self) -> bool:
        """True only when the content is durable in git. Push status is not part of it."""
        return self.status == COMMITTED


# --- pure: applying an operation to file content -----------------------------------

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def heading_index(lines: Sequence[str], title: str) -> int | None:
    """Index of the heading line titled `title`, or None. Pure, case-insensitive."""
    want = title.strip().casefold()
    for i, line in enumerate(lines):
        match = HEADING.match(line)
        if match and match.group(2).strip().casefold() == want:
            return i
    return None


def section_end(lines: Sequence[str], start: int) -> int:
    """Index just past the section opened at `start` — the next same-or-higher heading."""
    match = HEADING.match(lines[start])
    level = len(match.group(1)) if match else 1
    for i in range(start + 1, len(lines)):
        nxt = HEADING.match(lines[i])
        if nxt and len(nxt.group(1)) <= level:
            return i
    return len(lines)


def _entry_lines(text: str) -> list[str]:
    """An entry as lines, with surrounding blank lines dropped. Pure."""
    lines = text.strip("\n").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _separator(previous: str, first: str) -> list[str]:
    """The blank line between existing content and an inserted block — or nothing. Pure.

    Two consecutive list items belong to one list and must not be split by a blank line;
    everything else reads as a new paragraph and gets one. Keeping this rule here means
    the assistant never has to reason about markdown spacing when composing an entry.
    """
    if not previous.strip() or not first.strip():
        return []  # one side is already blank; a second blank line would be noise
    if LIST_ITEM.match(previous) and LIST_ITEM.match(first):
        return []
    return [""]


def _insert_after(lines: list[str], at: int, block: list[str]) -> list[str]:
    """Insert `block` at `at`, spaced from its neighbours per `_separator`."""
    lead = _separator(lines[at - 1], block[0]) if at > 0 else []
    tail = _separator(block[-1], lines[at]) if at < len(lines) else []
    return [*lines[:at], *lead, *block, *tail, *lines[at:]]


def _body_end(lines: Sequence[str], start: int, end: int) -> int:
    """Index just past the last non-blank line of a section body. Pure."""
    at = end
    while at > start + 1 and not lines[at - 1].strip():
        at -= 1
    return at


def _find_block(lines: Sequence[str], block: Sequence[str], lo: int, hi: int) -> int | None:
    """Index of the first run in [lo, hi) matching `block` line-for-line (stripped). Pure."""
    want = [line.strip() for line in block]
    if not want:
        return None
    for i in range(lo, hi - len(want) + 1):
        if [line.strip() for line in lines[i : i + len(want)]] == want:
            return i
    return None


def render(lines: Sequence[str]) -> str:
    """Lines back to file content: no trailing blank lines, exactly one final newline."""
    out = list(lines)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n" if out else ""


def apply_operation(current: str, op: Operation) -> str:
    """`current` + one operation -> the new file content. Pure.

    Raises `HubCommitError` when the operation cannot apply — a remove that matches
    nothing, or an unknown kind. Silently succeeding on a no-op is the failure mode that
    puts a commit message in history describing something that never happened.
    """
    lines = current.splitlines()
    if op.kind == APPEND_ENTRY:
        return _apply_append(lines, op)
    if op.kind == REPLACE_SECTION:
        return _apply_replace(lines, op)
    if op.kind == REMOVE_ENTRY:
        return _apply_remove(lines, op)
    raise HubCommitError(f"unknown operation {op.kind!r} — expected one of {', '.join(OPERATIONS)}")


def _apply_append(lines: list[str], op: Operation) -> str:
    block = _entry_lines(op.text)
    if not block:
        raise HubCommitError("nothing to append — the entry text is empty")
    if op.section is None:
        return render(_insert_after(lines, len(lines), block))
    start = heading_index(lines, op.section)
    if start is None:  # a hub grows sections as it grows domains; create it rather than fail
        return render(_insert_after(lines, len(lines), [f"## {op.section}", "", *block]))
    return render(_insert_after(lines, _body_end(lines, start, section_end(lines, start)), block))


def _apply_replace(lines: list[str], op: Operation) -> str:
    if not op.section:
        raise HubCommitError("replace-section needs a section name")
    block = _entry_lines(op.text)
    start = heading_index(lines, op.section)
    if start is None:
        return render(_insert_after(lines, len(lines), [f"## {op.section}", "", *block]))
    end = section_end(lines, start)
    return render([*lines[: start + 1], "", *block, "", *lines[end:]])


def _apply_remove(lines: list[str], op: Operation) -> str:
    block = _entry_lines(op.text)
    if not block:
        raise HubCommitError("nothing to remove — the entry text is empty")
    lo, hi = 0, len(lines)
    if op.section:
        start = heading_index(lines, op.section)
        if start is None:
            raise HubCommitError(f"no section titled {op.section!r} in {op.file}")
        lo, hi = start + 1, section_end(lines, start)
    at = _find_block(lines, block, lo, hi)
    if at is None:
        excerpt = block[0][:60]
        raise HubCommitError(f"no entry matching {excerpt!r} in {op.file} — nothing was removed")
    kept = [*lines[:at], *lines[at + len(block) :]]
    if at > 0 and at < len(kept) and not kept[at - 1].strip() and not kept[at].strip():
        kept.pop(at)  # do not leave a double blank line where the entry was
    return render(kept)


def added_lines(old: str, new: str) -> list[tuple[int, str]]:
    """(1-based line number, text) for lines in `new` that were not in `old`. Pure.

    Set-based rather than a diff on purpose: the scan must not re-flag content that was
    already in the file, or one pre-existing false positive would refuse every future
    write to that hub forever.
    """
    before = set(old.splitlines())
    return [(n, line) for n, line in enumerate(new.splitlines(), 1) if line not in before]


# --- pure: the credential scan ------------------------------------------------------

# Shapes, not identities. Every rule here matches something that is *only* ever a
# credential — never a hostname, handle, address, or account number, all of which a
# legitimate personal hub is supposed to contain.
TOKEN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key block", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("PuTTY private key", re.compile(r"PuTTY-User-Key-File")),
    ("github personal access token", re.compile(r"\bghp_[A-Za-z0-9]{20,}")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("github oauth/app/refresh token", re.compile(r"\bgh[osur]_[A-Za-z0-9]{20,}")),
    ("anthropic/openai api key", re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}")),
    ("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]{20,}")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("stripe secret key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid api key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}")),
    ("json web token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+")),
    ("pgp private key block", re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----")),
)

AUTHORIZATION = re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic|token)\s+(\S{8,})")

SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret(?:[_-]?key)?|access[_-]?token|auth[_-]?token|password|passwd"
    r"|client[_-]?secret|private[_-]?key|credential)\b\s*[:=]\s*[\"']?([^\s\"']{12,})"
)

# An opaque blob with no word structure. Length + alphabet + entropy together keep git
# shas (lowercase hex), UUIDs, URLs, and prose out.
OPAQUE = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])")

PLACEHOLDER = re.compile(
    r"(?i)^(?:<.*>|\$\{.*\}|\$[A-Z_]+|x{3,}|\*{3,}|\.{3,}|-{3,}|redacted|changeme|placeholder"
    r"|your[-_].*|example.*|none|null|n/?a|unset|todo.*|see[-_].*)$"
)

ASSIGNMENT_MIN_ENTROPY = 3.0
OPAQUE_MIN_ENTROPY = 4.5


def entropy(value: str) -> float:
    """Shannon entropy per character, in bits. Pure."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def looks_opaque(value: str) -> bool:
    """Whether a bare string is credential-shaped rather than a word, id, or hash. Pure.

    Requires all three character classes: a 40-char git sha and a lowercase-hex UUID both
    fail on "has an uppercase letter", which is what keeps ordinary hub content clean.
    """
    if len(value) < 32 or PLACEHOLDER.match(value):
        return False
    classes = (
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
    )
    return all(classes) and entropy(value) >= OPAQUE_MIN_ENTROPY


def scan_line(text: str) -> list[str]:
    """Rule names this one line trips. Pure. Empty for ordinary hub content."""
    hits = [name for name, pattern in TOKEN_RULES if pattern.search(text)]
    auth = AUTHORIZATION.search(text)
    if auth and not PLACEHOLDER.match(auth.group(1)):
        hits.append("Authorization header value")
    for _, value in SECRET_ASSIGNMENT.findall(text):
        if not PLACEHOLDER.match(value) and entropy(value) >= ASSIGNMENT_MIN_ENTROPY:
            hits.append("secret-shaped assignment")
            break
    if any(looks_opaque(candidate) for candidate in OPAQUE.findall(text)):
        hits.append("high-entropy opaque string")
    return hits


def scan_lines(numbered: Iterable[tuple[int, str]]) -> list[Finding]:
    """Findings over (line number, text) pairs. Pure. Values are never captured."""
    return [Finding(rule, n) for n, text in numbered for rule in scan_line(text)]


def scan_text(text: str) -> list[Finding]:
    """Findings over a whole document. Pure."""
    return scan_lines(enumerate(text.splitlines(), 1))


# --- pure: commit messages ----------------------------------------------------------


def commit_message(request: WriteRequest) -> str:
    """A message a reviewer can act on without ever opening the diff (R5, R6).

    Subject states the change; the body states what was touched and why; the trailers
    carry the instruction source (R6) and enough structure for the review loop to group
    changes by file without parsing prose.
    """
    op = request.operation
    subject = request.summary.strip().splitlines()[0] if request.summary.strip() else op.describe()
    reason = request.reason.strip() or "no reason recorded"
    return (
        f"{subject}\n\n"
        f"Changed: {op.describe()}.\n"
        f"Why: {reason}\n\n"
        f"{TRAILER_FILE}: {op.file}\n"
        f"{TRAILER_OPERATION}: {op.kind}\n"
        f"{TRAILER_SOURCE}: {request.source}\n"
    )


def reconcile_message(paths: Sequence[str]) -> str:
    """The message for content found uncommitted in the hub worktree at entry."""
    listed = "\n".join(f"  - {p}" for p in paths)
    return (
        "Reconcile uncommitted hub content\n\n"
        f"Changed: committed {len(paths)} file(s) found uncommitted in the hub working "
        "tree before this session's write.\n"
        "Why: content that only exists on disk is invisible to review and is lost with "
        "the host; nobody claims authorship of it, so it is recorded as unattributed "
        "rather than folded into the assistant's own commit.\n\n"
        f"{listed}\n\n"
        f"{TRAILER_SOURCE}: {UNATTRIBUTED}\n"
    )


def parse_porcelain(out: str) -> list[str]:
    """Paths from `git status --porcelain -z`. Pure.

    Rename and copy records are followed by their *source* path as a separate NUL record;
    both sides are returned, because both have to be committed for the tree to be clean.
    """
    records = [r for r in out.split("\0") if r]
    paths: list[str] = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        if ("R" in status or "C" in status) and i < len(records):
            paths.append(records[i])
            i += 1
        paths.append(path)
    return paths


# --- the exclusive lock -------------------------------------------------------------


def default_lock_path(hub: Path) -> Path:
    """Inside `.git/`, so the lock can never be committed or dirty the working tree."""
    return hub.joinpath(*LOCK_PARTS)


class HubLock:
    """`fcntl.flock` around read -> modify -> write -> scan -> commit.

    Two properties earn the choice of flock over a pid file: the kernel releases it when
    the holder dies (so an OOM-killed or SIGKILLed session cannot wedge every future
    write — `test_hub_commit` proves this with a real SIGKILL), and it is advisory per
    open file description, so two threads in one process serialize just as two processes
    do.

    The file's *content* is only a breadcrumb: the holder's pid, cleared on clean
    release. Finding a record after acquiring the lock therefore means the previous
    holder died — reported as `recovered_note`, never treated as a failure.
    """

    def __init__(
        self,
        path: Path,
        *,
        timeout: float = LOCK_TIMEOUT_SECONDS,
        poll: float = 0.02,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.clock = clock
        self.sleep = sleep
        self.recovered_pid: int | None = None
        self._fh = None

    @property
    def recovered_note(self) -> str:
        if self.recovered_pid is None:
            return ""
        return (
            f"cleared a stale lock record left by pid {self.recovered_pid} — that process "
            "is gone (the kernel releases flock on process death), so nothing was blocked"
        )

    def __enter__(self) -> HubLock:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a+")
        except OSError as e:
            raise HubCommitError(f"cannot open the hub write lock at {self.path}: {e}") from e
        deadline = self.clock() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if self.clock() >= deadline:
                    holder = self._recorded_holder()
                    self._fh.close()
                    self._fh = None
                    raise LockBusy(
                        f"another hub write held the lock at {self.path} for more than "
                        f"{self.timeout:g}s{holder} — nothing was written"
                    ) from None
                self.sleep(self.poll)
        self.recovered_pid = self._read_pid()
        self._stamp()
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()  # a clean release leaves no record behind
            self._fh.flush()
        except OSError:
            pass
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def _read_pid(self) -> int | None:
        try:
            self._fh.seek(0)
            first = self._fh.read().split()
            return int(first[0]) if first else None
        except (OSError, ValueError):
            return None

    def _recorded_holder(self) -> str:
        try:
            first = self.path.read_text().split()
            return f" (held by pid {first[0]})" if first else ""
        except OSError:
            return ""

    def _stamp(self) -> None:
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(f"{os.getpid()} {datetime.now(UTC).isoformat(timespec='seconds')}\n")
            self._fh.flush()
        except OSError:
            pass  # the breadcrumb is diagnostics; the lock itself is already held


# --- filesystem ---------------------------------------------------------------------


def atomic_write(path: Path, content: str) -> None:
    """Write via a temp file in the same directory, then `Path.replace()`.

    Mirrors `trust/provenance.py`: a concurrent reader sees either the old file or the
    new one, never a half-written hub. The temp name carries the pid so a crashed run
    leaves an obviously-foreign file rather than colliding with a real hub file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.buzai-tmp.{os.getpid()}")
    try:
        temp.write_text(content)
        temp.replace(path)
    except OSError:
        temp.unlink(missing_ok=True)
        raise


def roll_back(path: Path, prior: str | None) -> None:
    """Undo a write: restore the previous content, or remove a file we just created."""
    if prior is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, prior)


def hub_relative(hub: Path, name: str) -> Path:
    """The target path, refusing anything that escapes the hub or reaches into `.git`."""
    if not name or Path(name).is_absolute():
        raise HubCommitError(f"{name!r} must be a path relative to the hub repo")
    target = (hub / name).resolve()
    root = hub.resolve()
    if not target.is_relative_to(root):
        raise HubCommitError(f"{name!r} resolves outside the hub repo ({root})")
    if ".git" in target.relative_to(root).parts:
        raise HubCommitError(f"{name!r} is inside the git directory — hub content never goes there")
    return hub / name


# --- git ----------------------------------------------------------------------------


def run_git(
    hub: Path,
    args: Sequence[str],
    runner: GitRunner,
    timeout: float = GIT_TIMEOUT_SECONDS,
    env: dict[str, str | None] | None = None,
) -> GitResult:
    """Every call targets the repo with `-C`, never an inherited cwd."""
    return runner(["-C", str(hub), *args], env or LOCAL_ENV, timeout)


def identity_env(identity: tuple[str, str], now: datetime) -> dict[str, str | None]:
    """Author/committer for one invocation. Never `git config` — global state stays put.

    Supplying the identity here also means a box with no `user.name` configured can still
    record knowledge, instead of failing every write on a setup step nobody performed.
    """
    name, email = identity
    stamp = now.isoformat(timespec="seconds")
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
        "GIT_COMMITTER_DATE": stamp,
    }


def _detail(result: GitResult) -> str:
    text = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return text.splitlines()[-1]


def commit_paths(
    hub: Path,
    paths: Sequence[str],
    message: str,
    identity: tuple[str, str],
    runner: GitRunner,
    now: datetime,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> str:
    """Stage and commit exactly `paths`; return the new sha. Raises on any failure.

    Path-scoped on purpose: a file left dirty because it tripped the credential scan must
    not be dragged into this commit.
    """
    staged = run_git(hub, ["add", "--", *paths], runner, timeout)
    if staged.returncode != 0:
        raise HubCommitError(f"git add failed: {_detail(staged)}")
    env = identity_env(identity, now)
    committed = run_git(hub, ["commit", "-q", "-m", message, "--", *paths], runner, timeout, env)
    if committed.returncode != 0:
        raise HubCommitError(f"git commit failed: {_detail(committed)}")
    head = run_git(hub, ["rev-parse", "HEAD"], runner, timeout)
    if head.returncode != 0:
        raise HubCommitError(f"cannot read HEAD after committing: {_detail(head)}")
    return head.stdout.strip()


def dirty_paths(hub: Path, runner: GitRunner, timeout: float = GIT_TIMEOUT_SECONDS) -> list[str]:
    """Paths with uncommitted changes. Raises when the repo cannot be inspected."""
    result = run_git(hub, ["status", "--porcelain", "-z"], runner, timeout)
    if result.returncode != 0:
        raise HubCommitError(f"cannot read the hub working tree state: {_detail(result)}")
    return parse_porcelain(result.stdout)


def current_branch(hub: Path, runner: GitRunner, timeout: float = GIT_TIMEOUT_SECONDS) -> str:
    result = run_git(hub, ["rev-parse", "--abbrev-ref", "HEAD"], runner, timeout)
    branch = result.stdout.strip()
    return branch if result.returncode == 0 and branch and branch != "HEAD" else "main"


def scan_worktree(hub: Path, paths: Sequence[str]) -> dict[str, tuple[Finding, ...]]:
    """Credential findings per dirty path. A deleted or unreadable path contributes none.

    An untracked *directory* is reported by git as one entry, so it is expanded here —
    otherwise its files would be committed unscanned.
    """
    findings: dict[str, tuple[Finding, ...]] = {}
    for name in paths:
        target = hub / name
        files = sorted(target.rglob("*")) if target.is_dir() else [target]
        hits: list[Finding] = []
        for file in files:
            if not file.is_file():
                continue
            try:
                hits.extend(scan_text(file.read_text()))
            except (OSError, UnicodeDecodeError):
                continue  # binary or unreadable: nothing to match, and not our business
        if hits:
            findings[name] = tuple(hits)
    return findings


def reconcile_dirty(
    hub: Path,
    runner: GitRunner,
    now: datetime,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Reconciliation:
    """Commit pre-existing uncommitted content as unattributed, so review can see it."""
    paths = dirty_paths(hub, runner, timeout)
    if not paths:
        return Reconciliation()
    flagged = scan_worktree(hub, paths)
    committable = [p for p in paths if p not in flagged]
    skipped = tuple((p, flagged[p]) for p in paths if p in flagged)
    if not committable:
        return Reconciliation(None, (), skipped)
    sha = commit_paths(
        hub,
        committable,
        reconcile_message(committable),
        UNATTRIBUTED_IDENTITY,
        runner,
        now,
        timeout,
    )
    return Reconciliation(sha, tuple(committable), skipped)


# --- the push backlog ---------------------------------------------------------------


def default_backlog_path(hub: Path) -> Path:
    """Inside `.git/`, next to the lock: durable, per-instance, never committed."""
    return hub.joinpath(*BACKLOG_PARTS)


def read_backlog(path: Path) -> Backlog:
    """The recorded backlog, or an empty one. Every malformed state reads as empty."""
    try:
        data = json.loads(path.read_text())
        pending = tuple(
            (str(item["commit"]), datetime.fromisoformat(item["recorded_at"]))
            for item in data.get("pending", [])
        )
        attempt = data.get("last_attempt")
        return Backlog(
            pending,
            str(data.get("last_error", "")),
            datetime.fromisoformat(attempt) if attempt else None,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return Backlog()


def write_backlog(path: Path, backlog: Backlog) -> bool:
    """Record the backlog atomically. False when it could not be written (never fatal).

    Refuses to create the `.git` directory itself — a stray one turns a plain directory
    into a repo git then reports as broken (the rule `hub_remote.write_cache` follows).
    """
    if not path.parent.parent.is_dir():
        return False
    payload = {
        "pending": [
            {"commit": sha, "recorded_at": at.isoformat(timespec="seconds")}
            for sha, at in backlog.pending
        ],
        "last_error": backlog.last_error,
        "last_attempt": (
            backlog.last_attempt.isoformat(timespec="seconds") if backlog.last_attempt else None
        ),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, json.dumps(payload, indent=2) + "\n")
    except OSError:
        return False
    return True


def record_unpushed(path: Path, commit: str | None, now: datetime, error: str) -> Backlog:
    """Add `commit` (if new) to the backlog and stamp the failed attempt."""
    current = read_backlog(path)
    pending = list(current.pending)
    if commit and commit not in [sha for sha, _ in pending]:
        pending.append((commit, now))
    backlog = Backlog(tuple(pending), error, now)
    write_backlog(path, backlog)
    return backlog


def clear_backlog(path: Path, now: datetime) -> Backlog:
    """Everything on the branch reached the remote, so nothing is pending any more."""
    backlog = Backlog((), "", now)
    write_backlog(path, backlog)
    return backlog


# --- push ---------------------------------------------------------------------------

Verifier = Callable[[Path], Verification]


def default_verifier(hub: Path) -> Verification:
    """Privacy verification for the real remote. Injected in tests so they never dial out."""
    return verify_remote(hub, runner=default_git_runner, now=datetime.now(UTC))


def is_divergence(result: GitResult) -> bool:
    """Whether a failed push means the remote holds commits this repo does not. Pure."""
    text = f"{result.stderr}\n{result.stdout}".lower()
    return any(pattern in text for pattern in DIVERGENCE_PATTERNS)


def attempt_push(
    hub: Path,
    *,
    runner: GitRunner,
    verifier: Verifier,
    now: datetime,
    commit: str | None = None,
    backlog_path: Path | None = None,
    timeout: float = PUSH_TIMEOUT_SECONDS,
) -> PushOutcome:
    """Push the current branch, outside the lock and bounded by `timeout`.

    Never fatal. Every non-success records the commit in the backlog so the next write —
    or `--retry-push` at service start — tries again. Divergence is called out
    separately: it is the one failure a retry cannot fix, and the one where the tempting
    remedies (rebase, merge, `--force`) are all forbidden.
    """
    path = backlog_path or default_backlog_path(hub)
    verification = verifier(hub)
    if verification.verdict == LOCAL_ONLY:
        return PushOutcome(
            PUSH_LOCAL_ONLY,
            "no remote is configured — the commit is durable locally but not off-box",
            record_unpushed(path, commit, now, "no remote configured"),
        )
    if not verification.push_allowed:
        detail = f"push refused: {verification.detail}"
        return PushOutcome(PUSH_REFUSED, detail, record_unpushed(path, commit, now, detail))

    remote = verification.remote or "origin"
    branch = current_branch(hub, runner)
    result = run_git(
        hub,
        [*NO_PROXY_ARGS, "push", remote, f"HEAD:refs/heads/{branch}"],
        runner,
        timeout,
        authenticated_env(),
    )
    if result.returncode == 0:
        return PushOutcome(
            PUSHED, f"pushed to {remote} ({verification.url})", clear_backlog(path, now)
        )
    detail = _detail(result)
    if is_divergence(result):
        halt = (
            f"remote {remote} has diverged from this hub — it holds commits this repo does "
            f"not ({detail}). HALTED: nothing was merged, rebased, or force-pushed. Remote "
            "content is never pulled into hub files, and your commits are never discarded; "
            "resolve it by hand"
        )
        return PushOutcome(DIVERGED, halt, record_unpushed(path, commit, now, halt))
    failure = f"push to {remote} failed: {detail}"
    return PushOutcome(PUSH_FAILED, failure, record_unpushed(path, commit, now, failure))


def retry_push(
    hub: Path,
    *,
    runner: GitRunner = default_git_runner,
    verifier: Verifier = default_verifier,
    now: datetime | None = None,
    backlog_path: Path | None = None,
    timeout: float = PUSH_TIMEOUT_SECONDS,
) -> PushOutcome:
    """Retry the backlog — what `--retry-push` runs at service start.

    A write's own push needs no separate retry: pushing the branch carries every
    unpushed commit with it, which is why a successful push clears the whole backlog.
    """
    moment = now or datetime.now(UTC)
    path = backlog_path or default_backlog_path(hub)
    backlog = read_backlog(path)
    if backlog.count == 0:
        return PushOutcome(NOTHING_TO_PUSH, "no unpushed hub commits recorded", backlog)
    return attempt_push(
        hub, runner=runner, verifier=verifier, now=moment, backlog_path=path, timeout=timeout
    )


# --- the write ----------------------------------------------------------------------


def record(
    hub: Path,
    request: WriteRequest,
    *,
    now: datetime,
    runner: GitRunner = default_git_runner,
    verifier: Verifier = default_verifier,
    push: bool = True,
    lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    git_timeout: float = GIT_TIMEOUT_SECONDS,
    push_timeout: float = PUSH_TIMEOUT_SECONDS,
    lock_path: Path | None = None,
    backlog_path: Path | None = None,
) -> WriteResult:
    """Apply one operation and commit it; then, outside the lock, try to push.

    Never raises: every failure comes back as a `WriteResult` whose `status` is `failed`
    or `refused`, because a caller that mistakes an exception for "probably fine" is how
    a lost fact becomes invisible. `status == COMMITTED` means and only means the content
    is durable in git — the push is reported separately and is allowed to fail.
    """
    op = request.operation
    if request.source not in SOURCES:
        return WriteResult(FAILED, None, op.file, f"unknown instruction source {request.source!r}")

    reconciled = Reconciliation()
    lock_note = ""
    try:
        with HubLock(
            lock_path or default_lock_path(hub), timeout=lock_timeout
        ) as lock:  # read -> modify -> write -> scan -> commit
            lock_note = lock.recovered_note
            target = hub_relative(hub, op.file)
            reconciled = reconcile_dirty(hub, runner, now, git_timeout)

            # The reconciler left this very file dirty because its *existing* content is
            # credential-shaped. Proceeding would path-scope the commit onto that path and
            # carry the flagged content in with the new entry, past a scan that only ever
            # looks at added lines. Refuse before touching the file: nothing is written,
            # nothing is committed, and the flagged content stays where the reconciler
            # left it — uncommitted, and reported.
            blocking = reconciled.blocking(op.file)
            if blocking:
                findings = tuple(f for _, hits in blocking for f in hits)
                flagged = ", ".join(p for p, _ in blocking)
                return WriteResult(
                    REFUSED,
                    None,
                    op.file,
                    f"refused to record into {op.file}: {flagged} is already sitting "
                    "uncommitted with credential-shaped content "
                    f"({'; '.join(str(f) for f in findings)}), and this write commits that "
                    f"path — the pre-existing content would be committed with it. Nothing "
                    f"was written and nothing was committed; remove or redact the flagged "
                    f"content in {flagged} first, then record this again",
                    findings=findings,
                    reconciled=reconciled,
                    lock_note=lock_note,
                )

            prior = target.read_text() if target.is_file() else None
            new = apply_operation(prior or "", op)
            if new == (prior or ""):
                raise HubCommitError(
                    f"the operation would not change {op.file} — nothing was recorded"
                )
            atomic_write(target, new)

            # From here the file on disk is ahead of git, so every exit path below puts
            # it back: the commit is the durability boundary, and content that failed to
            # reach it must not linger on disk to be swept up later as `unattributed`.
            try:
                findings = tuple(scan_lines(added_lines(prior or "", new)))
                if findings:
                    roll_back(target, prior)
                    return WriteResult(
                        REFUSED,
                        None,
                        op.file,
                        f"refused to commit {op.file}: the new content is credential-shaped "
                        f"({'; '.join(str(f) for f in findings)}); {op.file} was rolled back "
                        "to its previous content and nothing was committed",
                        findings=findings,
                        reconciled=reconciled,
                        lock_note=lock_note,
                    )
                sha = commit_paths(
                    hub,
                    [op.file],
                    commit_message(request),
                    ASSISTANT_IDENTITY,
                    runner,
                    now,
                    git_timeout,
                )
            except (HubCommitError, OSError):
                roll_back(target, prior)
                raise
    except (HubCommitError, OSError) as e:
        return WriteResult(
            FAILED, None, op.file, str(e), reconciled=reconciled, lock_note=lock_note
        )

    outcome = PushOutcome(PUSH_SKIPPED, "push not attempted")
    if push:
        outcome = attempt_push(
            hub,
            runner=runner,
            verifier=verifier,
            now=now,
            commit=sha,
            backlog_path=backlog_path,
            timeout=push_timeout,
        )
    return WriteResult(
        COMMITTED,
        sha,
        op.file,
        f"committed {sha[:8]}: {op.describe()}",
        push=outcome,
        reconciled=reconciled,
        lock_note=lock_note,
    )


# --- the verb -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub_commit.py",
        description="Record one hub change as one reviewable commit, then push it off-box.",
    )
    parser.add_argument("--file", help="hub file to change, relative to the hub repo")
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--append", metavar="TEXT", help="append an entry ('-' reads stdin)")
    what.add_argument("--replace-section", metavar="TEXT", help="replace --section's body")
    what.add_argument("--remove-entry", metavar="TEXT", help="remove a matching entry")
    parser.add_argument("--section", help="scope the operation to this heading")
    parser.add_argument("--summary", default="", help="subject line: what changed")
    parser.add_argument("--reason", default="", help="why it changed (goes in the message body)")
    parser.add_argument(
        "--source",
        choices=CLI_SOURCES,
        default=AUTONOMOUS,
        help=(
            "who asked for it: 'autonomous' (the assistant's own judgement) or "
            "'owner-directed' (the owner asked for this content). To record a CORRECTION "
            "to something already in the hub, reject the original instead — "
            "`hub_review.py --reject <id> --reason ... [--value ...]` — which removes the "
            "wrong content, records the owner's value, and links the two. A correction "
            "recorded here would never appear for review"
        ),
    )
    parser.add_argument("--no-push", action="store_true", help="commit only; do not push")
    parser.add_argument(
        "--retry-push",
        action="store_true",
        help="push any unpushed commits and exit (run this at service start)",
    )
    return parser


def read_text_arg(value: str) -> str:
    """`-` means stdin, so multi-line entries do not have to survive shell quoting."""
    return sys.stdin.read() if value == "-" else value


def operation_from(args: argparse.Namespace) -> Operation:
    if not args.file:
        raise HubCommitError("--file is required")
    if args.append is not None:
        return Operation(APPEND_ENTRY, args.file, read_text_arg(args.append), args.section)
    if args.replace_section is not None:
        return Operation(
            REPLACE_SECTION, args.file, read_text_arg(args.replace_section), args.section
        )
    if args.remove_entry is not None:
        return Operation(REMOVE_ENTRY, args.file, read_text_arg(args.remove_entry), args.section)
    raise HubCommitError("one of --append / --replace-section / --remove-entry is required")


def report_push(outcome: PushOutcome, now: datetime) -> None:
    """Print the push result. Only divergence is loud; the rest are durability notes."""
    if outcome.status == PUSHED:
        print(f"hub-commit: {outcome.detail}")
        return
    if outcome.status == DIVERGED:
        print(f"hub-commit HALTED: {outcome.detail}", file=sys.stderr)
    elif outcome.status != PUSH_SKIPPED:
        print(f"hub-commit: not pushed — {outcome.detail}", file=sys.stderr)
    age = outcome.backlog.oldest_age_seconds(now)
    if outcome.backlog.count:
        oldest = f", oldest {age / 3600:.1f}h old" if age is not None else ""
        print(
            f"hub-commit: {outcome.backlog.count} commit(s) unpushed{oldest} — the write is "
            "durable locally and will be retried on the next write and at service start",
            file=sys.stderr,
        )


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = hub_dir()
    except HubPathError as e:
        print(f"hub-commit FAIL: {e}", file=sys.stderr)
        return 1
    print(f"hub-commit: hubs resolve to {location}")
    if not (location.path / ".git").exists():
        print(
            f"hub-commit FAIL: {location.path} is not a hub repo yet — run `make hub-init`",
            file=sys.stderr,
        )
        return 1

    now = datetime.now(UTC)
    if args.retry_push:
        outcome = retry_push(location.path, now=now)
        report_push(outcome, now)
        if outcome.status == NOTHING_TO_PUSH:
            print("hub-commit: nothing to push — no unpushed hub commits recorded")
        # local-only exits 0 for the same reason `hub_remote.check` does: no remote is a
        # degraded-durability condition (U6 warns), not a failure to fix before starting.
        return 0 if outcome.ok or outcome.status == PUSH_LOCAL_ONLY else 1

    try:
        request = WriteRequest(operation_from(args), args.summary, args.reason, args.source)
    except HubCommitError as e:
        print(f"hub-commit FAIL: {e}", file=sys.stderr)
        return 1

    result = record(location.path, request, now=now, push=not args.no_push)
    if result.lock_note:
        print(f"hub-commit: {result.lock_note}")
    if result.reconciled.commit:
        print(
            f"hub-commit: committed {len(result.reconciled.committed)} pre-existing "
            f"uncommitted file(s) as {UNATTRIBUTED} ({result.reconciled.commit[:8]}) so they "
            "are visible to review"
        )
    for path, findings in result.reconciled.skipped:
        # "left uncommitted" has to be true of the path it names. `record()` refuses the
        # write when its target is one of these, so a committed path can no longer appear
        # here — this keeps the claim true by construction rather than by reading that.
        if result.recorded and covers(path, result.file):
            continue
        print(
            f"hub-commit WARN: left {path} uncommitted — it is credential-shaped "
            f"({'; '.join(str(f) for f in findings)})",
            file=sys.stderr,
        )
    if result.status != COMMITTED:
        print(f"hub-commit FAIL: {result.detail}", file=sys.stderr)
        return 1
    print(f"hub-commit: {result.detail}")
    report_push(result.push, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
