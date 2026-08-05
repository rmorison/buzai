"""Show the owner what the assistant recorded, take a verdict per item, and resolve it.

The review loop runs entirely through conversation (R12): the assistant reads this
module's output, relays it in plain language, and calls back with the owner's verdict.
There is no dashboard, no inbox, and no second surface to keep in sync.

Why review state is a git note, not a JSON file
-----------------------------------------------
A shared mutable JSON file of dispositions is defective either way you commit to it.
Left uncommitted, the R11 audit trail dies with the host, and an R8 restore onto a fresh
instance resurfaces every already-approved item as pending — the owner re-reviews their
whole history. Committed, two sessions attached to the same hub write conflicting
versions of one file and the loser's dispositions vanish (or, worse, land as a conflict
marker inside the record of what was approved).

A note per commit under `refs/notes/buzai-review` has neither problem: entries are keyed
by commit sha, so two sessions disposing different items touch different blobs and merge
without textual conflict, and the ref pushes and restores with the repo. `git notes add`
also refuses to overwrite an existing note, which makes double-application a git-level
impossibility rather than a check this module has to get right.

The notes ref must be pushed and fetched explicitly
---------------------------------------------------
`git push` does not carry `refs/notes/*`, and `git clone` does not fetch it. **This
module owns that end to end** — `dispose()` pushes the ref after writing a note, and
`--push-notes` retries it (wired into `make hub-push`, so a service start drains both
backlogs). It is not folded into U4's push path deliberately: `hub_commit` would have to
know about a ref that only this unit defines, and the ordering matters — a note must
never reach the remote before the correction commit it refers to. Restoring on a fresh
instance needs one extra fetch, documented in `CLAUDE.md`:

    git fetch origin "refs/notes/*:refs/notes/*"

What "what changed" returns
---------------------------
Undisposed commits and their *message text* — never a diff (R9). The owner reviews from
a phone, and a hunk is not a fact. Items the owner does not act on stay pending: nothing
here advances an item because its neighbours were approved (R10), and there is no
"last reviewed at" marker anywhere in the design — a time window would silently sweep
past everything the owner scrolled by without deciding.

Commits with no `instruction-source` trailer are not review items: they were not recorded
through the assistant's write path (`make hub-init`'s seed, or the owner's own git
commits from another machine). Correction commits are excluded too — they *are* the
owner's verdict, and asking them to review their own correction is a loop.

What a rejection does
---------------------
It carries the owner's reason and *optionally* a correct value.

  * **with a value** — the recorded content is removed and the owner's value is recorded
    in its place.
  * **without one** — the recorded content is **removed**. Not re-derived, not softened,
    not cosmetically edited and marked corrected. The writing session is gone and the
    source material with it, so any replacement this module produced would be invented,
    and an invented "correction" is worse than the original error: it is wrong *and*
    carries the owner's approval.

Removal locates the content by the lines the commit added (read from the commit itself —
that is machine-internal; R9 governs what the *owner* sees). If those lines are no longer
in the file — reworded, superseded, or already gone — the rejection resolves as
`already-absent` rather than failing or guessing which current lines descended from them.
That is what makes a rejection of a twenty-commit-old change well defined no matter how
much the file has moved since. A rejected change that *removed* content resolves as
`nothing-to-remove` for the same reason: putting the text back is a judgement about what
the hub should say, and only the owner makes those — `--value` is how they do it.

Corrections are recorded through `hub_commit.record()`, never written here directly, so
they get the same lock, credential scan, atomic write, and trailer discipline as anything
else that touches a hub. A replacement is two commits (remove, then record the owner's
value) because a single one would mean handing `record()` pre-rendered file content —
exactly the read-outside-the-lock race U4 exists to prevent.

The staleness warning
---------------------
The summary leads with a warning when the oldest unpushed commit is older than
`STALE_BACKLOG_SECONDS`. The review loop is the one surface the owner looks at often, so
it is where a silent push backlog has to surface — no scheduler, no digest, no second
notification path.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hub_commit import (  # noqa: E402
    APPEND_ENTRY,
    DIVERGED,
    GIT_TIMEOUT_SECONDS,
    NOTHING_TO_PUSH,
    OWNER_CORRECTION,
    PUSH_ENV,
    PUSH_FAILED,
    PUSH_LOCAL_ONLY,
    PUSH_REFUSED,
    PUSH_TIMEOUT_SECONDS,
    PUSHED,
    REMOVE_ENTRY,
    TRAILER_FILE,
    TRAILER_OPERATION,
    TRAILER_SOURCE,
    Backlog,
    HubLock,
    Operation,
    Verifier,
    WriteRequest,
    default_backlog_path,
    default_lock_path,
    default_verifier,
    identity_env,
    is_divergence,
    read_backlog,
    record,
    run_git,
)
from scripts.hub_paths import HubPathError, hub_dir  # noqa: E402
from scripts.hub_remote import LOCAL_ONLY, GitRunner, default_git_runner  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]

# One ref, named for this system rather than the generic `refs/notes/commits`, so it
# cannot collide with notes anyone else attaches to the same repo.
NOTES_REF = "refs/notes/buzai-review"
NOTE_SCHEMA = 1

# Dispositions. `owner-authored` is not a verdict the owner gives — it is what this
# module stamps on the correction commits it creates, so they are traceable to the
# rejection that caused them without ever entering the review queue.
APPROVED = "approved"
REJECTED = "rejected"
OWNER_AUTHORED = "owner-authored"

# How a rejection was resolved.
REMOVED = "removed"
REPLACED = "replaced"
ALREADY_ABSENT = "already-absent"
NOTHING_TO_REMOVE = "nothing-to-remove"

# Disposition outcomes.
RECORDED = "recorded"
ALREADY_DISPOSED = "already-disposed"
FAILED = "failed"

# The identity that authors the notes-ref commits. The note's *content* records who
# decided; this only keeps the notes history distinguishable from hub content in a log.
REVIEW_IDENTITY = ("buzai review", "review@buzai.invalid")

# When the review summary leads with a push-backlog warning.
#
# 24h is chosen so that the ordinary transients — a reboot, an overnight network drop, a
# router that came back at breakfast — do not cry wolf, while the condition that actually
# matters (an expired deploy key or token, which is the *expected* steady-state failure
# for fine-grained credentials) surfaces on the first review of the next day. A full day
# is also about as much knowledge as it is tolerable to lose with the host: past that,
# "it is only on this box" stops being a footnote.
STALE_BACKLOG_SECONDS = 24 * 3600.0

LOG_FORMAT = "%H%x1f%aI%x1f%an%x1f%B%x1e"

TRAILER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9-]*):\s*(.+?)\s*$")
# `hub_commit.Operation.describe()` is the only thing that quotes a name in the "Changed:"
# line, and it quotes exactly one: the section. Used purely to place a replacement under
# the same heading — never to decide what is removed.
QUOTED_SECTION = re.compile(r"'([^']+)'")

# `git log` on a branch with no commits is a legitimate empty state, not a broken repo.
NO_COMMITS = "does not have any commits yet"
NO_NOTE = "no note found"


class HubReviewError(Exception):
    """Review state could not be read or written. Never degraded into a clean report."""


# --- what the owner is shown ---------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One recorded hub change, as review sees it: prose and provenance, no diff."""

    sha: str
    when: datetime
    author: str
    subject: str
    changed: str
    why: str
    file: str
    operation: str
    source: str

    @property
    def short(self) -> str:
        return self.sha[:8]

    @property
    def section(self) -> str | None:
        """The heading the change went under, if its description names one. A hint only."""
        match = QUOTED_SECTION.search(self.changed)
        return match.group(1) if match else None

    def plain(self, now: datetime, index: int) -> list[str]:
        """The item in plain language (R9). Pure — the caller prints it."""
        lines = [f"{index}. [{self.short}] {age(seconds_between(self.when, now))} — {self.subject}"]
        if self.changed:
            lines.extend(wrapped("what", self.changed))
        if self.why:
            lines.extend(wrapped("why", self.why))
        facts = [f"file: {self.file}" if self.file else "", f"recorded: {self.source}"]
        lines.append("     " + " | ".join(f for f in facts if f))
        return lines


@dataclass(frozen=True)
class Disposition:
    """What the owner decided about one commit, as stored in its note."""

    disposition: str
    decided_at: datetime
    reason: str = ""
    resolution: str = ""
    corrections: tuple[str, ...] = ()
    corrects: str = ""
    by: str = "owner"

    def to_json(self) -> str:
        payload = {
            "schema": NOTE_SCHEMA,
            "disposition": self.disposition,
            "decided_at": self.decided_at.isoformat(timespec="seconds"),
            "by": self.by,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.resolution:
            payload["resolution"] = self.resolution
        if self.corrections:
            payload["corrections"] = list(self.corrections)
        if self.corrects:
            payload["corrects"] = self.corrects
        return json.dumps(payload, indent=2, sort_keys=True)


def parse_disposition(text: str, sha: str) -> Disposition:
    """A stored note back into a `Disposition`. Raises rather than guessing.

    A note that cannot be parsed is a corrupted review record. Skipping it would put the
    item back in the queue as if it had never been decided, and defaulting it to approved
    would hide content the owner never saw — so it is reported instead.
    """
    try:
        data = json.loads(text)
        return Disposition(
            disposition=str(data["disposition"]),
            decided_at=datetime.fromisoformat(str(data["decided_at"])),
            reason=str(data.get("reason", "")),
            resolution=str(data.get("resolution", "")),
            corrections=tuple(str(c) for c in data.get("corrections", ())),
            corrects=str(data.get("corrects", "")),
            by=str(data.get("by", "owner")),
        )
    except (ValueError, TypeError, KeyError) as e:
        raise HubReviewError(
            f"the review note on {sha[:8]} is not readable ({e}) — refusing to report it as "
            f"either pending or decided; inspect it with `git notes --ref={NOTES_REF} show {sha}`"
        ) from e


@dataclass(frozen=True)
class Decision:
    """The owner's verdict on one commit, as handed to `dispose()`."""

    sha: str
    disposition: str
    reason: str = ""
    value: str = ""
    section: str | None = None


@dataclass(frozen=True)
class NotesPush:
    """What the (privacy-gated) push of the notes ref did. Never fatal."""

    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status in (PUSHED, NOTHING_TO_PUSH)


@dataclass(frozen=True)
class DispositionResult:
    """The outcome of one verdict: what was decided and what it did to the hub."""

    status: str
    sha: str
    disposition: str
    detail: str
    resolution: str = ""
    corrections: tuple[str, ...] = ()
    notes: NotesPush = field(default_factory=lambda: NotesPush(NOTHING_TO_PUSH, "not attempted"))

    @property
    def ok(self) -> bool:
        return self.status == RECORDED


# --- pure: parsing the log -----------------------------------------------------------


def parse_trailers(message: str) -> dict[str, str]:
    """Trailers from a commit message's last paragraph. Pure.

    Requires a body: a one-paragraph message whose only line happens to read
    `feat: something` is a subject, not a trailer block, and reading it as one would
    invent provenance that was never recorded.
    """
    paragraphs = [p for p in message.strip().split("\n\n") if p.strip()]
    if len(paragraphs) < 2:
        return {}
    matched = [TRAILER_LINE.match(line) for line in paragraphs[-1].splitlines() if line.strip()]
    if not matched or not all(matched):
        return {}
    return {m.group(1).lower(): m.group(2) for m in matched if m is not None}


def body_field(message: str, label: str) -> str:
    """The value of a `Label: …` line in the message body. Pure. Empty when absent."""
    for line in message.splitlines():
        if line.startswith(label):
            return line[len(label) :].strip()
    return ""


def build_change(sha: str, when: str, author: str, message: str) -> Change:
    """One log record -> a `Change`. Pure."""
    trailers = parse_trailers(message)
    lines = message.strip().splitlines()
    return Change(
        sha=sha.strip(),
        when=datetime.fromisoformat(when.strip()),
        author=author.strip(),
        subject=lines[0].strip() if lines else "(no message)",
        changed=body_field(message, "Changed:"),
        why=body_field(message, "Why:"),
        file=trailers.get(TRAILER_FILE, ""),
        operation=trailers.get(TRAILER_OPERATION, ""),
        source=trailers.get(TRAILER_SOURCE, ""),
    )


def parse_log(out: str) -> list[Change]:
    """`git log --pretty=LOG_FORMAT` output -> changes, newest first. Pure."""
    changes = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        try:
            sha, when, author, message = chunk.strip("\n").split("\x1f", 3)
        except ValueError as e:
            raise HubReviewError(f"cannot parse the hub log record {chunk[:40]!r}: {e}") from e
        changes.append(build_change(sha, when, author, message))
    return changes


def reviewable(change: Change) -> bool:
    """Whether this commit is something the owner reviews. Pure.

    Yes for anything the assistant's write path recorded — including the `unattributed`
    reconciliation of content found loose on disk, which is precisely the content that
    would otherwise never be seen. No for commits with no instruction source (the store's
    own initialization, or the owner's git commits from another machine) and no for
    corrections, which are the owner's verdict rather than something awaiting one.
    """
    return bool(change.source) and change.source != OWNER_CORRECTION


# --- pure: presentation --------------------------------------------------------------


def seconds_between(then: datetime, now: datetime) -> float:
    """Non-negative age in seconds; a clock that moved backwards reads as 0. Pure."""
    return max(0.0, (now - then).total_seconds())


def duration(seconds: float) -> str:
    """A rough human duration — the precision a review summary can act on. Pure."""
    if seconds < 90:
        return "under a minute"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f}m"
    if seconds < 48 * 3600:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def age(seconds: float) -> str:
    return "just now" if seconds < 90 else f"{duration(seconds)} ago"


def wrapped(label: str, text: str, width: int = 96) -> list[str]:
    """A labelled prose line, wrapped with a hanging indent. Pure.

    The owner reads this on a narrow screen, and an unwrapped `Why:` sentence is where a
    plain-language summary stops being readable and starts being a wall.
    """
    head = f"     {label}: ".ljust(11)
    return textwrap.wrap(
        text, width=width, initial_indent=head, subsequent_indent=" " * len(head)
    ) or [head + text]


def staleness_warning(
    backlog: Backlog, now: datetime, threshold: float = STALE_BACKLOG_SECONDS
) -> str | None:
    """The line the summary leads with when the push backlog has gone stale. Pure."""
    seconds = backlog.oldest_age_seconds(now)
    if seconds is None or seconds < threshold:
        return None
    return (
        f"hub-review WARNING: {backlog.count} hub commit(s) have not reached the private "
        f"remote in {duration(seconds)} — that history exists only on this box, so losing "
        "the host loses it. Run `make hub-push`; if it keeps failing, the deploy key or "
        "token has most likely expired (they do, routinely)."
    )


def summarize(
    pending: Sequence[Change],
    backlog: Backlog,
    now: datetime,
    *,
    total: int,
    hidden: int = 0,
    threshold: float = STALE_BACKLOG_SECONDS,
) -> list[str]:
    """The whole "what changed" report. Pure — plain language, never a diff (R9)."""
    lines: list[str] = []
    warning = staleness_warning(backlog, now, threshold)
    if warning:
        lines.extend([warning, ""])

    if not pending:
        if total:
            lines.append(
                f"hub-review: nothing is awaiting your review — all {total} recorded "
                "change(s) have been approved or rejected."
            )
        else:
            lines.append("hub-review: nothing has been recorded into the hub yet.")
        return lines

    more = f" (showing the newest {len(pending)} of {len(pending) + hidden})" if hidden else ""
    lines.append(
        f"hub-review: {len(pending) + hidden} change(s) awaiting review{more}, newest first."
    )
    for index, change in enumerate(pending, 1):
        lines.append("")
        lines.extend(change.plain(now, index))
    lines.extend(
        [
            "",
            "Approve or reject each one. Anything you do not act on stays pending.",
            "  approve:  scripts/hub_review.py --approve <id>",
            '  reject:   scripts/hub_review.py --reject <id> --reason "why it is wrong"',
            "  ...rejecting removes the recorded content; add "
            '--value "the correct value" to replace it instead.',
        ]
    )
    return lines


def contains_block(current: str, block: Sequence[str]) -> bool:
    """Whether `block` still appears, line for line, in `current`. Pure.

    Mirrors the matcher `hub_commit._apply_remove` uses — contiguous, compared stripped —
    so this decision and the removal that acts on it agree. If they ever disagree,
    `record()` refuses with "nothing was removed" and the rejection is reported as failed;
    the one outcome that is not possible is a silent no-op marked as resolved.
    """
    want = [line.strip() for line in block]
    if not want:
        return False
    lines = [line.strip() for line in current.splitlines()]
    return any(lines[i : i + len(want)] == want for i in range(len(lines) - len(want) + 1))


# --- git: reading history and dispositions -------------------------------------------


def detail_of(result) -> str:
    """The most useful single line of a failed git call."""
    text = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    return text.splitlines()[-1]


def read_changes(
    hub: Path, *, runner: GitRunner = default_git_runner, timeout: float = GIT_TIMEOUT_SECONDS
) -> list[Change]:
    """Every commit in the hub, newest first. Raises when history cannot be read."""
    result = run_git(hub, ["log", f"--pretty=format:{LOG_FORMAT}"], runner, timeout)
    if result.returncode != 0:
        if NO_COMMITS in result.stderr:
            return []  # an initialized-but-empty hub is a legitimate state, not a failure
        raise HubReviewError(f"cannot read the hub history: {detail_of(result)}")
    return parse_log(result.stdout)


def disposed_shas(
    hub: Path, *, runner: GitRunner = default_git_runner, timeout: float = GIT_TIMEOUT_SECONDS
) -> set[str]:
    """Commits that already carry a disposition. Raises if the notes ref is unreadable.

    A ref that does not exist yet is empty, not broken: `git notes list` reports no notes
    and exits 0, which correctly means "nothing has been disposed" — every item pending,
    which is the safe direction. A ref that exists but cannot be read is a different
    thing entirely and is never smoothed over: read as "nothing disposed" it re-opens
    every settled item, and read the other way it would mark unreviewed content as
    approved. Neither is allowed to pass as a clean report.
    """
    result = run_git(hub, ["notes", f"--ref={NOTES_REF}", "list"], runner, timeout)
    if result.returncode != 0:
        raise HubReviewError(
            f"cannot read review dispositions from {NOTES_REF}: {detail_of(result)} — refusing "
            "to report anything as reviewed or pending until that ref is readable"
        )
    return {line.split()[-1] for line in result.stdout.splitlines() if line.strip()}


def read_disposition(
    hub: Path,
    sha: str,
    *,
    runner: GitRunner = default_git_runner,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Disposition | None:
    """This commit's disposition, or None when it has not been decided."""
    result = run_git(hub, ["notes", f"--ref={NOTES_REF}", "show", sha], runner, timeout)
    if result.returncode != 0:
        if NO_NOTE in result.stderr.lower():
            return None
        raise HubReviewError(f"cannot read the review note on {sha[:8]}: {detail_of(result)}")
    return parse_disposition(result.stdout, sha)


def write_disposition(
    hub: Path,
    sha: str,
    disposition: Disposition,
    *,
    now: datetime,
    runner: GitRunner = default_git_runner,
    timeout: float = GIT_TIMEOUT_SECONDS,
    lock_path: Path | None = None,
) -> None:
    """Attach the disposition to `sha`. Raises if one is already there.

    Two guards, and neither is redundant.

    The U4 write lock is what makes concurrent verdicts safe *as a pair*. Notes on
    different commits do not conflict textually, but the ref update behind them is not a
    compare-and-swap: two simultaneous `git notes add` calls each build a new notes tree
    from the same parent and the second one wins, and **both report success**. Measured
    on real git: ten notes added as five overlapping pairs left five notes and exit code
    0 every time. A lost verdict that reports success is the worst outcome available
    here — the owner is never told their decision evaporated — so the write is serialized
    through the same lock U4 uses for hub content.

    The `add` (never `append`, never `--force`) is what makes a *repeated* verdict
    impossible: git refuses outright when a note exists, so even a verdict arriving from
    outside this lock cannot overwrite a decision the owner already made.
    """
    with HubLock(lock_path or default_lock_path(hub)):
        existing = read_disposition(hub, sha, runner=runner, timeout=timeout)
        if existing is not None:
            raise HubReviewError(
                f"{sha[:8]} was already {existing.disposition} at "
                f"{existing.decided_at.isoformat(timespec='seconds')} — not overwriting it"
            )
        result = run_git(
            hub,
            ["notes", f"--ref={NOTES_REF}", "add", "-m", disposition.to_json(), sha],
            runner,
            timeout,
            identity_env(REVIEW_IDENTITY, now),
        )
        if result.returncode != 0:
            raise HubReviewError(f"cannot record the disposition of {sha[:8]}: {detail_of(result)}")


def find_change(
    hub: Path,
    sha: str,
    *,
    runner: GitRunner = default_git_runner,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> Change:
    """The change identified by `sha` (short forms accepted). Raises when there is none."""
    result = run_git(hub, ["log", "-1", f"--pretty=format:{LOG_FORMAT}", sha], runner, timeout)
    if result.returncode != 0 or not result.stdout.strip():
        raise HubReviewError(f"no hub commit matches {sha!r} — nothing was decided")
    return parse_log(result.stdout)[0]


def added_block(
    hub: Path,
    sha: str,
    file: str,
    *,
    runner: GitRunner = default_git_runner,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> list[str]:
    """The lines this commit added to `file`.

    Read from the commit's own diff, which is machine-internal: R9 governs what the owner
    is shown, and prose cannot locate text to remove. The lines are used only as a
    literal search key against the *current* file — never reverse-applied, so a file that
    has moved on since does not make the rejection unresolvable.
    """
    args = ["show", "--format=", "--unified=0", "--no-color", sha, "--", file]
    result = run_git(hub, args, runner, timeout)
    if result.returncode != 0:
        raise HubReviewError(f"cannot read what {sha[:8]} changed in {file}: {detail_of(result)}")
    added = [
        line[1:]
        for line in result.stdout.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    while added and not added[0].strip():
        added.pop(0)
    while added and not added[-1].strip():
        added.pop()
    return added


# --- pushing the notes ref -----------------------------------------------------------


def notes_ref_exists(hub: Path, runner: GitRunner, timeout: float) -> bool:
    result = run_git(hub, ["rev-parse", "--verify", "--quiet", NOTES_REF], runner, timeout)
    return result.returncode == 0 and bool(result.stdout.strip())


def push_notes(
    hub: Path,
    *,
    runner: GitRunner = default_git_runner,
    verifier: Verifier = default_verifier,
    timeout: float = PUSH_TIMEOUT_SECONDS,
) -> NotesPush:
    """Push `refs/notes/buzai-review`, under the same privacy gate as hub content.

    Dispositions carry the owner's words about their own knowledge base, so they are no
    less personal than the hub files, and they go nowhere that has not been proven
    private. Failure is never fatal: the note is durable locally and the next disposition
    or `make hub-push` carries it. Divergence halts exactly as it does in U4 — no merge,
    no force — because a remote that can rewrite dispositions is a path for hiding
    content from review.
    """
    if not notes_ref_exists(hub, runner, GIT_TIMEOUT_SECONDS):
        return NotesPush(NOTHING_TO_PUSH, "no review dispositions have been recorded yet")
    verification = verifier(hub)
    if verification.verdict == LOCAL_ONLY:
        return NotesPush(PUSH_LOCAL_ONLY, "no remote is configured — dispositions stay on this box")
    if not verification.push_allowed:
        return NotesPush(PUSH_REFUSED, f"notes push refused: {verification.detail}")

    remote = verification.remote or "origin"
    result = run_git(hub, ["push", remote, f"{NOTES_REF}:{NOTES_REF}"], runner, timeout, PUSH_ENV)
    if result.returncode == 0:
        return NotesPush(PUSHED, f"review dispositions pushed to {remote} ({verification.url})")
    detail = detail_of(result)
    if is_divergence(result):
        return NotesPush(
            DIVERGED,
            f"{NOTES_REF} on {remote} holds dispositions this box does not ({detail}). HALTED: "
            "nothing was merged or force-pushed. Reconcile by hand with `git fetch "
            f"{remote} '{NOTES_REF}:refs/notes/remote-review'` and `git notes merge`",
        )
    return NotesPush(PUSH_FAILED, f"could not push {NOTES_REF} to {remote}: {detail}")


# --- taking a verdict ----------------------------------------------------------------


def rejection_reason(change: Change, reason: str, tail: str) -> str:
    """The `Why:` text carried by a correction commit, so history says why (R11). Pure.

    Three lines: the owner's words first, then what was done about them, then the commit
    being corrected. `rejected-commit` sits in the message *body* rather than the trailer
    block — `hub_commit.commit_message` owns the trailers and this module does not reach
    into them — so it is grep-able but not a git trailer. The authoritative link between
    a rejection and its correction is the note on each, in both directions.
    """
    said = reason.strip()
    if said and said[-1] not in ".!?":
        said += "."
    return (
        f"the owner rejected {change.short} ({change.subject!r}): {said}\n"
        f"{tail}\n"
        f"rejected-commit: {change.sha}"
    )


def _correct(
    hub: Path,
    operation: Operation,
    summary: str,
    reason: str,
    *,
    now: datetime,
    push: bool,
    **passthrough,
) -> str:
    """Record one correction through U4's write path. Returns the sha; raises on failure.

    Nothing in this module writes a hub file itself: going through `record()` is what
    gives a correction the same lock, credential scan, atomic write, and trailers as
    every other hub change — and what keeps a correction from racing a concurrent
    autonomous write.
    """
    result = record(
        hub,
        WriteRequest(operation, summary, reason, source=OWNER_CORRECTION),
        now=now,
        push=push,
        **passthrough,
    )
    if not result.recorded or result.commit is None:
        raise HubReviewError(f"the correction was not recorded: {result.detail}")
    return result.commit


def dispose(
    hub: Path,
    decision: Decision,
    *,
    now: datetime,
    runner: GitRunner = default_git_runner,
    verifier: Verifier = default_verifier,
    push: bool = True,
    timeout: float = GIT_TIMEOUT_SECONDS,
    push_timeout: float = PUSH_TIMEOUT_SECONDS,
    lock_path: Path | None = None,
    backlog_path: Path | None = None,
) -> DispositionResult:
    """Apply the owner's verdict to one commit: record it, and resolve a rejection.

    Order matters. The correction is committed *first* and the note written after, so a
    failure between them leaves the item pending — the owner is asked again, which is
    tedious but honest. The other order would mark an item decided while the content it
    condemned was still sitting in the hub.
    """
    passthrough = {
        "runner": runner,
        "verifier": verifier,
        "lock_path": lock_path,
        "backlog_path": backlog_path,
        "git_timeout": timeout,
        "push_timeout": push_timeout,
    }
    try:
        change = find_change(hub, decision.sha, runner=runner, timeout=timeout)
        existing = read_disposition(hub, change.sha, runner=runner, timeout=timeout)
        if existing is not None:
            return DispositionResult(
                ALREADY_DISPOSED,
                change.sha,
                existing.disposition,
                f"{change.short} was already {existing.disposition} at "
                f"{existing.decided_at.isoformat(timespec='seconds')} — nothing was applied twice",
                resolution=existing.resolution,
                corrections=existing.corrections,
            )
        if decision.disposition == APPROVED:
            note = Disposition(APPROVED, now)
            write_disposition(
                hub, change.sha, note, now=now, runner=runner, timeout=timeout, lock_path=lock_path
            )
            detail = f"{change.short} approved: {change.subject}"
            return _finish(hub, change.sha, APPROVED, detail, runner, verifier, push, push_timeout)

        if decision.disposition != REJECTED:
            raise HubReviewError(
                f"unknown disposition {decision.disposition!r} — expected "
                f"{APPROVED!r} or {REJECTED!r}"
            )
        if not decision.reason.strip():
            raise HubReviewError(
                "a rejection must carry the owner's reason — it is what the correction "
                "commit records, and the only account of why the content went away"
            )
        resolution, corrections, detail = _reject(
            hub, change, decision, now=now, push=push, passthrough=passthrough, runner=runner
        )
        note = Disposition(
            REJECTED,
            now,
            reason=decision.reason.strip(),
            resolution=resolution,
            corrections=corrections,
        )
        write_disposition(
            hub, change.sha, note, now=now, runner=runner, timeout=timeout, lock_path=lock_path
        )
        for sha in corrections:
            write_disposition(
                hub,
                sha,
                Disposition(
                    OWNER_AUTHORED, now, reason=decision.reason.strip(), corrects=change.sha
                ),
                now=now,
                runner=runner,
                timeout=timeout,
                lock_path=lock_path,
            )
        return _finish(
            hub,
            change.sha,
            REJECTED,
            detail,
            runner,
            verifier,
            push,
            push_timeout,
            resolution=resolution,
            corrections=corrections,
        )
    except (HubReviewError, OSError) as e:
        # OSError too: an unreadable hub file must come back as a failed verdict, not as a
        # traceback that leaves the caller guessing whether anything was applied.
        return DispositionResult(FAILED, decision.sha, decision.disposition, str(e))


def _reject(
    hub: Path,
    change: Change,
    decision: Decision,
    *,
    now: datetime,
    push: bool,
    passthrough: dict,
    runner: GitRunner,
) -> tuple[str, tuple[str, ...], str]:
    """Remove the rejected content and, if the owner supplied one, record their value."""
    if not change.file:
        raise HubReviewError(
            f"{change.short} names no hub file (no {TRAILER_FILE} trailer), so there is no "
            "recorded content to remove — it was not written through the hub write path"
        )
    target = hub / change.file
    current = target.read_text() if target.is_file() else ""
    block = added_block(hub, change.sha, change.file, runner=runner)
    value = decision.value.strip()
    corrections: list[str] = []

    if contains_block(current, block):
        corrections.append(
            _correct(
                hub,
                Operation(REMOVE_ENTRY, change.file, "\n".join(block)),
                f"Remove content the owner rejected ({change.short})",
                rejection_reason(
                    change,
                    decision.reason.strip(),
                    "The content was removed rather than re-derived: the writing session and "
                    "its source material are gone, so any replacement would be invented."
                    if not value
                    else "The owner supplied the correct value, recorded separately.",
                ),
                now=now,
                push=push and not value,
                **passthrough,
            )
        )
        resolution = REMOVED
        detail = f"{change.short} rejected — the recorded content was removed from {change.file}"
    elif not block:
        # The rejected change took content out rather than putting any in. Putting it back
        # is not this module's call: only the owner knows whether the removal was the
        # mistake or the wording was, and `--value` is how they say so.
        resolution = NOTHING_TO_REMOVE
        detail = (
            f"{change.short} rejected — that change added nothing to {change.file} (it removed "
            "or reorganized content), so there is nothing to take out. Nothing was restored "
            "automatically; if something went away that should not have, say what it should "
            "say and it will be recorded"
        )
    else:
        resolution = ALREADY_ABSENT
        detail = (
            f"{change.short} rejected — its content is no longer in {change.file} (reworded, "
            "superseded, or already removed), so nothing was removed and nothing was invented "
            "to stand in for it. If what is there now is also wrong, reject the change that "
            "wrote it"
        )

    if value:
        section = decision.section or change.section
        corrections.append(
            _correct(
                hub,
                Operation(APPEND_ENTRY, change.file, value, section=section),
                f"Record the owner's correction to {change.file} ({change.short})",
                rejection_reason(
                    change,
                    decision.reason.strip(),
                    "This is the value the owner supplied; nothing here was derived.",
                ),
                now=now,
                push=push,
                **passthrough,
            )
        )
        resolution = REPLACED
        detail = f"{change.short} rejected — replaced in {change.file} with the owner's value"
    return resolution, tuple(corrections), detail


def _finish(
    hub: Path,
    sha: str,
    disposition: str,
    detail: str,
    runner: GitRunner,
    verifier: Verifier,
    push: bool,
    push_timeout: float,
    *,
    resolution: str = "",
    corrections: tuple[str, ...] = (),
) -> DispositionResult:
    """Push the notes ref (best effort) and assemble the result."""
    notes = NotesPush(NOTHING_TO_PUSH, "notes push not attempted")
    if push:
        notes = push_notes(hub, runner=runner, verifier=verifier, timeout=push_timeout)
    return DispositionResult(
        RECORDED,
        sha,
        disposition,
        detail,
        resolution=resolution,
        corrections=corrections,
        notes=notes,
    )


# --- the verb ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub_review.py",
        description="Show hub changes awaiting review, and record the owner's verdict on one.",
    )
    what = parser.add_mutually_exclusive_group()
    what.add_argument("--approve", metavar="SHA", help="mark one change approved")
    what.add_argument("--reject", metavar="SHA", help="reject one change (needs --reason)")
    what.add_argument(
        "--push-notes",
        action="store_true",
        help="retry pushing the review dispositions ref and exit (run at service start)",
    )
    parser.add_argument("--reason", default="", help="the owner's reason for rejecting")
    parser.add_argument(
        "--value",
        default="",
        help="the correct value, in the owner's words — replaces instead of removing",
    )
    parser.add_argument("--section", help="heading to record --value under (default: the original)")
    parser.add_argument(
        "-n", "--limit", type=int, default=10, help="how many pending items to show"
    )
    parser.add_argument("--no-push", action="store_true", help="do not push anything off-box")
    return parser


def report_notes(notes: NotesPush) -> None:
    if notes.status == PUSHED:
        print(f"hub-review: {notes.detail}")
    elif notes.status == DIVERGED:
        print(f"hub-review HALTED: {notes.detail}", file=sys.stderr)
    elif notes.status not in (NOTHING_TO_PUSH,):
        print(
            f"hub-review: dispositions not pushed — {notes.detail}; they are durable locally "
            "and `make hub-push` will retry",
            file=sys.stderr,
        )


def run_list(hub: Path, limit: int, now: datetime) -> int:
    changes = read_changes(hub)
    noted = disposed_shas(hub)
    items = [c for c in changes if reviewable(c)]
    waiting = [c for c in items if c.sha not in noted]
    shown = waiting[:limit] if limit and limit > 0 else waiting
    print(
        "\n".join(
            summarize(
                shown,
                read_backlog(default_backlog_path(hub)),
                now,
                total=len(items),
                hidden=len(waiting) - len(shown),
            )
        )
    )
    return 0


def run_disposition(hub: Path, args: argparse.Namespace, now: datetime) -> int:
    decision = Decision(
        sha=args.approve or args.reject,
        disposition=APPROVED if args.approve else REJECTED,
        reason=args.reason,
        value=args.value,
        section=args.section,
    )
    result = dispose(hub, decision, now=now, push=not args.no_push)
    if result.status == FAILED:
        print(f"hub-review FAIL: {result.detail}", file=sys.stderr)
        return 1
    if result.status == ALREADY_DISPOSED:
        print(f"hub-review: {result.detail}")
        return 0
    print(f"hub-review: {result.detail}")
    for sha in result.corrections:
        print(f"hub-review: correction committed as {sha[:8]}")
    report_notes(result.notes)
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        location = hub_dir()
    except HubPathError as e:
        print(f"hub-review FAIL: {e}", file=sys.stderr)
        return 1
    print(f"hub-review: hubs resolve to {location}")
    hub = location.path
    if not (hub / ".git").exists():
        print(
            f"hub-review FAIL: {hub} is not a hub repo yet — run `make hub-init`", file=sys.stderr
        )
        return 1

    now = datetime.now(UTC)
    try:
        if args.push_notes:
            notes = push_notes(hub)
            report_notes(notes)
            if notes.status == NOTHING_TO_PUSH:
                print("hub-review: no review dispositions are waiting to be pushed")
            # local-only is a durability condition, not a startup failure — the same rule
            # `hub_commit --retry-push` follows.
            return 0 if notes.ok or notes.status == PUSH_LOCAL_ONLY else 1
        if args.approve or args.reject:
            return run_disposition(hub, args, now)
        return run_list(hub, args.limit, now)
    except HubReviewError as e:
        print(f"hub-review FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
