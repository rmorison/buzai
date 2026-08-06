import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.hub_commit import (
    APPEND_ENTRY,
    AUTONOMOUS,
    DIVERGED,
    NOTHING_TO_PUSH,
    OWNER_CORRECTION,
    PUSH_LOCAL_ONLY,
    PUSH_REFUSED,
    PUSHED,
    REMOVE_ENTRY,
    REPLACE_SECTION,
    UNATTRIBUTED,
    Backlog,
    Operation,
    WriteRequest,
    default_backlog_path,
    record,
    record_unpushed,
)
from scripts.hub_remote import INDETERMINATE, LOCAL_ONLY, PRIVATE, GitResult, Verification
from scripts.hub_remote import default_git_runner as real_git
from scripts.hub_review import (
    ALREADY_ABSENT,
    ALREADY_DISPOSED,
    APPROVED,
    FAILED,
    NOTES_REF,
    NOTHING_TO_REMOVE,
    OWNER_AUTHORED,
    RECORDED,
    REJECTED,
    REMOVED,
    REPLACED,
    STALE_BACKLOG_SECONDS,
    Change,
    Decision,
    Disposition,
    HubReviewError,
    build_change,
    contains_block,
    dispose,
    disposed_shas,
    duration,
    main,
    parse_disposition,
    parse_trailers,
    push_notes,
    read_changes,
    read_disposition,
    reviewable,
    run_list,
    staleness_warning,
    summarize,
)
from scripts.tests.env_isolation import assert_injection_suppressed, plant

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)

# Hermetic git: nothing from ~/.gitconfig or /etc/gitconfig reaches these tests, and no
# test ever touches the network or the real ~/hubs.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "buzai tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "buzai tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}
_SAVED: dict[str, str | None] = {}


def setUpModule():
    for key, value in GIT_ENV.items():
        _SAVED[key] = os.environ.get(key)
        os.environ[key] = value


def tearDownModule():
    for key, value in _SAVED.items():
        restore_env(key, value)


def restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def git(*args, check=True) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True, check=check)
    return r.stdout


def verifier_for(verdict=PRIVATE, remote="origin", url="ssh://host.invalid/hubs.git"):
    verification = Verification(verdict, remote, url, f"{verdict} (injected)", "probe")
    return lambda hub: verification


PRIVATE_REMOTE = verifier_for(PRIVATE)
NO_REMOTE = verifier_for(LOCAL_ONLY, remote=None)
UNVERIFIED_REMOTE = verifier_for(INDETERMINATE)


def local_verifier(remote: Path):
    """A verified-private verdict pointing at a real bare repo in the tempdir."""
    return verifier_for(PRIVATE, remote="origin", url=str(remote))


DRIVER = """
import sys, time
sys.path.insert(0, {repo!r})
from datetime import UTC, datetime
from pathlib import Path
from scripts.hub_review import REJECTED, Decision, dispose

hub, sha, fact, start_at = Path(sys.argv[1]), sys.argv[2], sys.argv[3], float(sys.argv[4])
while time.time() < start_at:
    time.sleep(0.002)
result = dispose(
    hub,
    Decision(sha, REJECTED, reason="the owner says %s is wrong" % fact),
    now=datetime.now(UTC),
    push=False,
)
print(result.status, result.resolution)
sys.exit(0 if result.status == "recorded" else 1)
"""

APPROVE_DRIVER = """
import sys, time
sys.path.insert(0, {repo!r})
from datetime import UTC, datetime
from pathlib import Path
from scripts.hub_review import APPROVED, Decision, dispose

hub, sha, start_at = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
while time.time() < start_at:
    time.sleep(0.002)
result = dispose(hub, Decision(sha, APPROVED), now=datetime.now(UTC), push=False)
print(result.status, result.detail)
sys.exit(0 if result.status == "recorded" else 1)
"""


class ReviewRepoCase(unittest.TestCase):
    """A real hub repo in a tempdir, written through the real U4 write path."""

    SEED = "# Notes\n\n- seeded fact\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hub = self.root / "hubs"
        self.hub.mkdir()
        git("init", "-q", "-b", "main", str(self.hub))
        self.notes = self.hub / "notes.md"
        self.notes.write_text(self.SEED)
        git("-C", str(self.hub), "add", "-A")
        git("-C", str(self.hub), "commit", "-q", "-m", "seed")

    def tearDown(self):
        self._tmp.cleanup()

    # --- writing changes to review -------------------------------------------------

    def append(self, fact: str, *, section=None, summary=None, source=AUTONOMOUS) -> str:
        result = record(
            self.hub,
            WriteRequest(
                Operation(APPEND_ENTRY, "notes.md", f"- {fact}", section=section),
                summary=summary or f"Record {fact}",
                reason=f"the assistant heard {fact} in conversation",
                source=source,
            ),
            now=NOW,
            push=False,
        )
        self.assertTrue(result.recorded, result.detail)
        assert result.commit is not None
        return result.commit

    def replace_section(self, section: str, text: str) -> str:
        result = record(
            self.hub,
            WriteRequest(
                Operation(REPLACE_SECTION, "notes.md", text, section=section),
                summary=f"Reword the {section} section",
                reason="the assistant tidied the wording later",
            ),
            now=NOW,
            push=False,
        )
        self.assertTrue(result.recorded, result.detail)
        assert result.commit is not None
        return result.commit

    # --- queries -------------------------------------------------------------------

    def pending(self) -> list[Change]:
        noted = disposed_shas(self.hub)
        return [c for c in read_changes(self.hub) if reviewable(c) and c.sha not in noted]

    def pending_subjects(self) -> list[str]:
        return [c.subject for c in self.pending()]

    def subjects(self) -> list[str]:
        return git("-C", str(self.hub), "log", "--pretty=%s").split("\n")

    def approve(self, sha: str, **kw):
        kw.setdefault("push", False)
        return dispose(self.hub, Decision(sha, APPROVED), now=NOW, **kw)

    def reject(self, sha: str, reason="that is wrong", value="", **kw):
        kw.setdefault("push", False)
        return dispose(self.hub, Decision(sha, REJECTED, reason=reason, value=value), now=NOW, **kw)

    def bare_remote(self) -> Path:
        remote = self.root / "remote.git"
        git("init", "-q", "--bare", "-b", "main", str(remote))
        git("-C", str(self.hub), "remote", "add", "origin", str(remote))
        return remote


# --- pure core -----------------------------------------------------------------------


class TestTrailerParsing(unittest.TestCase):
    MESSAGE = (
        "Record the boiler service\n\n"
        "Changed: appended a 1-line entry to home.md under 'House'.\n"
        "Why: the owner mentioned it in passing\n\n"
        "hub-file: home.md\nhub-operation: append-entry\ninstruction-source: autonomous\n"
    )

    def test_trailers_are_read_from_the_last_paragraph(self):
        self.assertEqual(
            parse_trailers(self.MESSAGE),
            {
                "hub-file": "home.md",
                "hub-operation": "append-entry",
                "instruction-source": "autonomous",
            },
        )

    def test_a_subject_only_message_has_no_trailers(self):
        # "feat: something" is a subject, not provenance — reading it as a trailer would
        # invent an instruction source that was never recorded
        self.assertEqual(parse_trailers("feat: add a thing\n"), {})

    def test_a_body_that_is_not_a_trailer_block_is_ignored(self):
        self.assertEqual(parse_trailers("Subject\n\nsome prose, not a trailer\n"), {})

    def test_a_change_carries_prose_and_provenance_but_no_diff(self):
        change = build_change(
            "abc1234def", "2026-08-05T10:00:00+00:00", "buzai assistant", self.MESSAGE
        )
        self.assertEqual(change.short, "abc1234d")
        self.assertEqual(change.subject, "Record the boiler service")
        self.assertEqual(change.changed, "appended a 1-line entry to home.md under 'House'.")
        self.assertEqual(change.why, "the owner mentioned it in passing")
        self.assertEqual(change.file, "home.md")
        self.assertEqual(change.source, "autonomous")
        self.assertEqual(change.section, "House")


class TestReviewable(unittest.TestCase):
    def change(self, source: str) -> Change:
        return Change("a" * 40, NOW, "buzai assistant", "s", "c", "w", "notes.md", "append", source)

    def test_assistant_recorded_changes_are_reviewed(self):
        for source in (AUTONOMOUS, "owner-directed", UNATTRIBUTED):
            self.assertTrue(reviewable(self.change(source)), source)

    def test_corrections_are_not_re_reviewed(self):
        self.assertFalse(reviewable(self.change(OWNER_CORRECTION)))

    def test_commits_with_no_instruction_source_are_not_review_items(self):
        self.assertFalse(reviewable(self.change("")))


class TestContainsBlock(unittest.TestCase):
    DOC = "# Notes\n\n- one\n- two\n- three\n"

    def test_a_present_block_is_found(self):
        self.assertTrue(contains_block(self.DOC, ["- two"]))

    def test_a_multi_line_block_must_be_contiguous(self):
        self.assertTrue(contains_block(self.DOC, ["- two", "- three"]))
        self.assertFalse(contains_block(self.DOC, ["- one", "- three"]))

    def test_reworded_content_is_not_a_match(self):
        self.assertFalse(contains_block(self.DOC, ["- two (reworded)"]))

    def test_an_empty_block_never_matches(self):
        self.assertFalse(contains_block(self.DOC, []))


class TestDispositionRoundTrip(unittest.TestCase):
    def test_a_rejection_round_trips_through_the_note(self):
        note = Disposition(
            REJECTED, NOW, reason="wrong number", resolution=REMOVED, corrections=("abc",)
        )
        back = parse_disposition(note.to_json(), "deadbeef")
        self.assertEqual(back, note)

    def test_an_unreadable_note_is_reported_not_guessed(self):
        for text in ("not json at all", '{"disposition": "approved"}', "{}"):
            with self.assertRaises(HubReviewError):
                parse_disposition(text, "deadbeef")


class TestSummary(unittest.TestCase):
    CHANGE = Change(
        "a" * 40,
        NOW - timedelta(hours=5),
        "buzai assistant",
        "Record the boiler service",
        "appended a 1-line entry to home.md under 'House'.",
        "the owner mentioned it in passing",
        "home.md",
        "append-entry",
        AUTONOMOUS,
    )

    def test_pending_items_are_plain_language_never_a_diff(self):
        text = "\n".join(summarize([self.CHANGE], Backlog(), NOW, total=1))
        self.assertIn("1 change(s) awaiting review", text)
        self.assertIn("Record the boiler service", text)
        self.assertIn("appended a 1-line entry to home.md", text)
        self.assertIn("5h ago", text)
        for marker in ("diff --git", "@@ ", "\n+", "\n-"):
            self.assertNotIn(marker, text)

    def test_nothing_pending_reports_cleanly(self):
        lines = summarize([], Backlog(), NOW, total=7)
        self.assertEqual(len(lines), 1)
        self.assertIn("nothing is awaiting your review", lines[0])
        self.assertIn("all 7 recorded change(s)", lines[0])

    def test_an_empty_hub_is_not_an_error_either(self):
        self.assertIn("nothing has been recorded", summarize([], Backlog(), NOW, total=0)[0])

    def test_a_truncated_list_says_how_many_are_hidden(self):
        text = "\n".join(summarize([self.CHANGE], Backlog(), NOW, total=9, hidden=8))
        self.assertIn("9 change(s) awaiting review (showing the newest 1 of 9)", text)

    def test_a_stale_backlog_leads_the_summary(self):
        backlog = Backlog(((("a" * 40), NOW - timedelta(days=3)),))
        lines = summarize([self.CHANGE], backlog, NOW, total=1)
        self.assertIn("WARNING", lines[0])
        self.assertIn("3d", lines[0])
        self.assertIn("make hub-push", lines[0])

    def test_a_fresh_backlog_does_not_warn(self):
        backlog = Backlog(((("a" * 40), NOW - timedelta(hours=1)),))
        self.assertIsNone(staleness_warning(backlog, NOW))
        self.assertNotIn("WARNING", "\n".join(summarize([], backlog, NOW, total=1)))

    def test_the_threshold_is_the_boundary(self):
        just_under = Backlog(((("a" * 40), NOW - timedelta(seconds=STALE_BACKLOG_SECONDS - 60)),))
        just_over = Backlog(((("a" * 40), NOW - timedelta(seconds=STALE_BACKLOG_SECONDS + 60)),))
        self.assertIsNone(staleness_warning(just_under, NOW))
        self.assertIsNotNone(staleness_warning(just_over, NOW))

    def test_long_prose_wraps_with_a_hanging_indent(self):
        change = Change(
            "b" * 40, NOW, "buzai assistant", "Subject", "x " * 80, "y " * 80, "h.md", "op", "auto"
        )
        lines = change.plain(NOW, 1)
        self.assertTrue(all(len(line) <= 100 for line in lines), lines)
        self.assertTrue([line for line in lines if line.startswith(" " * 11 + "x")])

    def test_durations_are_human(self):
        self.assertEqual(duration(30), "under a minute")
        self.assertEqual(duration(3600), "60m")
        self.assertEqual(duration(5 * 3600), "5h")
        self.assertEqual(duration(3 * 86400), "3d")


# --- what changed ---------------------------------------------------------------------


class TestWhatChanged(ReviewRepoCase):
    def setUp(self):
        super().setUp()
        self.first = self.append("the boiler was serviced")
        self.second = self.append("the dentist is on Oak Street")
        self.third = self.append("bin day is Tuesday")

    def test_every_recorded_change_is_pending_until_it_is_judged(self):
        self.assertEqual(len(self.pending()), 3)

    def test_the_seed_commit_is_not_a_review_item(self):
        # `make hub-init` and the owner's own git commits carry no instruction source
        self.assertNotIn("seed", self.pending_subjects())

    def test_items_arrive_newest_first_with_their_reasons(self):
        items = self.pending()
        self.assertEqual(items[0].subject, "Record bin day is Tuesday")
        self.assertIn("appended a 1-line entry to notes.md", items[0].changed)
        self.assertIn("the assistant heard bin day is Tuesday", items[0].why)
        self.assertEqual(items[0].source, AUTONOMOUS)

    def test_content_found_loose_on_disk_shows_up_as_unattributed(self):
        (self.hub / "home.md").write_text("# Home\n\n- typed straight into the file\n")
        self.append("something else")  # triggers U4's dirty-tree reconciliation
        sources = {c.source for c in self.pending()}
        self.assertIn(UNATTRIBUTED, sources)

    def test_the_listing_never_contains_a_diff(self):
        out = io.StringIO()
        with redirect_stdout(out):
            run_list(self.hub, 10, NOW)
        text = out.getvalue()
        self.assertIn("3 change(s) awaiting review", text)
        self.assertNotIn("diff --git", text)
        self.assertNotIn("@@", text)


class TestApproval(ReviewRepoCase):
    def setUp(self):
        super().setUp()
        self.first = self.append("the boiler was serviced")
        self.second = self.append("the dentist is on Oak Street")
        self.third = self.append("bin day is Tuesday")

    def test_an_approved_item_does_not_come_back(self):
        result = self.approve(self.second)
        self.assertEqual(result.status, RECORDED)
        self.assertNotIn(self.second, [c.sha for c in self.pending()])

    def test_approval_changes_no_hub_content(self):
        before = self.notes.read_text()
        head = git("-C", str(self.hub), "rev-parse", "HEAD").strip()
        self.approve(self.second)
        self.assertEqual(self.notes.read_text(), before)
        self.assertEqual(git("-C", str(self.hub), "rev-parse", "HEAD").strip(), head)

    def test_partial_review_leaves_the_rest_pending(self):
        self.approve(self.first)
        self.approve(self.third)
        remaining = self.pending()
        self.assertEqual([c.sha for c in remaining], [self.second])

    def test_the_disposition_is_stored_on_the_commit(self):
        self.approve(self.second)
        note = read_disposition(self.hub, self.second)
        self.assertIsNotNone(note)
        assert note is not None
        self.assertEqual(note.disposition, APPROVED)
        self.assertEqual(note.decided_at, NOW)

    def test_a_second_verdict_is_refused_rather_than_applied_twice(self):
        self.approve(self.second)
        again = dispose(
            self.hub, Decision(self.second, REJECTED, reason="changed my mind"), now=NOW, push=False
        )
        self.assertEqual(again.status, ALREADY_DISPOSED)
        self.assertEqual(again.disposition, APPROVED)
        self.assertIn("- the dentist is on Oak Street", self.notes.read_text())

    def test_a_short_sha_identifies_the_item(self):
        self.assertEqual(self.approve(self.third[:8]).status, RECORDED)


# --- rejection ------------------------------------------------------------------------


class TestRejectionRemoves(ReviewRepoCase):
    """Covers AE2: three changes, one rejected with a reason — the content goes, the
    correction is committed, and BOTH the original and the correction stay in history."""

    def setUp(self):
        super().setUp()
        self.first = self.append("the boiler was serviced")
        self.second = self.append("the dentist is on Oak Street")
        self.third = self.append("bin day is Tuesday")
        self.result = self.reject(self.second, reason="the dentist is on Elm Street")

    def test_the_rejected_content_is_removed_from_the_file(self):
        self.assertEqual(self.result.status, RECORDED)
        self.assertEqual(self.result.resolution, REMOVED)
        self.assertNotIn("Oak Street", self.notes.read_text())

    def test_the_other_facts_are_untouched(self):
        content = self.notes.read_text()
        self.assertIn("- the boiler was serviced", content)
        self.assertIn("- bin day is Tuesday", content)

    def test_the_original_commit_is_still_in_history(self):
        # R11: rejecting is not erasing — the record of what was recorded stays
        self.assertIn(self.second, git("-C", str(self.hub), "rev-list", "HEAD").split())
        self.assertIn("Record the dentist is on Oak Street", self.subjects())

    def test_the_correction_is_a_commit_of_its_own(self):
        self.assertEqual(len(self.result.corrections), 1)
        message = git("-C", str(self.hub), "log", "-1", "--pretty=%B", self.result.corrections[0])
        self.assertIn(f"instruction-source: {OWNER_CORRECTION}", message)
        self.assertIn("hub-operation: remove-entry", message)

    def test_the_owners_reason_travels_with_the_correction(self):
        message = git("-C", str(self.hub), "log", "-1", "--pretty=%B", self.result.corrections[0])
        self.assertIn("the dentist is on Elm Street", message)
        self.assertIn(f"rejected-commit: {self.second}", message)

    def test_nothing_was_invented_to_replace_it(self):
        message = git("-C", str(self.hub), "log", "-1", "--pretty=%B", self.result.corrections[0])
        self.assertIn("would be invented", message)
        self.assertNotIn("Elm Street", self.notes.read_text())

    def test_the_rejection_and_its_resolution_are_recorded(self):
        note = read_disposition(self.hub, self.second)
        assert note is not None
        self.assertEqual(note.disposition, REJECTED)
        self.assertEqual(note.reason, "the dentist is on Elm Street")
        self.assertEqual(note.resolution, REMOVED)
        self.assertEqual(note.corrections, self.result.corrections)

    def test_the_correction_points_back_at_what_it_corrects(self):
        note = read_disposition(self.hub, self.result.corrections[0])
        assert note is not None
        self.assertEqual(note.disposition, OWNER_AUTHORED)
        self.assertEqual(note.corrects, self.second)

    def test_neither_the_rejected_item_nor_the_correction_comes_back(self):
        waiting = [c.sha for c in self.pending()]
        self.assertNotIn(self.second, waiting)
        self.assertNotIn(self.result.corrections[0], waiting)
        self.assertEqual(sorted(waiting), sorted([self.first, self.third]))

    def test_a_rejection_with_no_reason_is_refused(self):
        result = self.reject(self.first, reason="   ")
        self.assertEqual(result.status, FAILED)
        self.assertIn("must carry the owner's reason", result.detail)
        self.assertIn("- the boiler was serviced", self.notes.read_text())


class TestRejectionWithAValue(ReviewRepoCase):
    def setUp(self):
        super().setUp()
        self.sha = self.append("the dentist is on Oak Street", section="Health")
        self.result = self.reject(
            self.sha, reason="wrong street", value="- the dentist is on Elm Street"
        )

    def test_the_owners_value_replaces_the_rejected_content(self):
        self.assertEqual(self.result.status, RECORDED)
        self.assertEqual(self.result.resolution, REPLACED)
        content = self.notes.read_text()
        self.assertNotIn("Oak Street", content)
        self.assertIn("- the dentist is on Elm Street", content)

    def test_the_replacement_lands_under_the_original_heading(self):
        content = self.notes.read_text()
        self.assertIn("## Health\n\n- the dentist is on Elm Street", content)

    def test_removal_and_replacement_are_both_in_history(self):
        self.assertEqual(len(self.result.corrections), 2)
        for sha in self.result.corrections:
            message = git("-C", str(self.hub), "log", "-1", "--pretty=%B", sha)
            self.assertIn(f"instruction-source: {OWNER_CORRECTION}", message)
            self.assertIn("wrong street", message)

    def test_the_note_records_that_it_was_replaced(self):
        note = read_disposition(self.hub, self.sha)
        assert note is not None
        self.assertEqual(note.resolution, REPLACED)
        self.assertEqual(len(note.corrections), 2)


class TestStaleRejection(ReviewRepoCase):
    """Rejecting a change from twenty commits ago whose text was reworded since. The
    removal must resolve without needing the original hunk to still apply, and must not
    guess which of the current lines descended from the rejected one."""

    def setUp(self):
        super().setUp()
        self.sha = self.append("the dentist is on Oak Street", section="Health")
        for n in range(20):
            self.append(f"unrelated fact {n}")
        self.replace_section("Health", "- the dentist is Dr. Smith, on Oak St.")
        self.before = self.notes.read_text()
        self.result = self.reject(self.sha, reason="I never said that")

    def test_the_rejection_resolves_rather_than_failing(self):
        self.assertEqual(self.result.status, RECORDED)
        self.assertEqual(self.result.resolution, ALREADY_ABSENT)

    def test_nothing_was_removed_and_nothing_was_invented(self):
        self.assertEqual(self.notes.read_text(), self.before)
        self.assertEqual(self.result.corrections, ())

    def test_the_owner_is_told_what_happened_and_what_to_do_next(self):
        self.assertIn("no longer in notes.md", self.result.detail)
        self.assertIn("reject the change that wrote it", self.result.detail)

    def test_the_verdict_is_still_recorded_with_its_reason(self):
        note = read_disposition(self.hub, self.sha)
        assert note is not None
        self.assertEqual(note.disposition, REJECTED)
        self.assertEqual(note.reason, "I never said that")
        self.assertNotIn(self.sha, [c.sha for c in self.pending()])

    def test_rejecting_the_rewording_removes_what_is_actually_there(self):
        rewording = [c for c in self.pending() if "Reword" in c.subject][0]
        result = self.reject(rewording.sha, reason="wrong too")
        self.assertEqual(result.resolution, REMOVED)
        self.assertNotIn("Dr. Smith", self.notes.read_text())


class TestRejectingARemoval(ReviewRepoCase):
    """Rejecting a change that took content *out*. There is nothing to remove, and the
    removed text is not put back on this module's initiative — restoring it is a value
    judgement only the owner can make, which is what `--value` is for."""

    def setUp(self):
        super().setUp()
        self.append("the boiler was serviced")
        result = record(
            self.hub,
            WriteRequest(
                Operation(REMOVE_ENTRY, "notes.md", "- seeded fact"),
                summary="Drop the seeded fact",
                reason="the assistant judged it obsolete",
            ),
            now=NOW,
            push=False,
        )
        self.removal = result.commit

    def test_it_resolves_as_nothing_to_remove(self):
        result = self.reject(self.removal, reason="I still need that")
        self.assertEqual(result.status, RECORDED)
        self.assertEqual(result.resolution, NOTHING_TO_REMOVE)
        self.assertEqual(result.corrections, ())
        self.assertIn("added nothing to notes.md", result.detail)

    def test_the_owners_value_is_what_puts_content_back(self):
        result = self.reject(self.removal, reason="I still need that", value="- seeded fact")
        self.assertEqual(result.resolution, REPLACED)
        self.assertIn("- seeded fact", self.notes.read_text())


class TestRejectionFailurePaths(ReviewRepoCase):
    def test_an_unknown_commit_is_refused(self):
        result = self.reject("0" * 40, reason="whatever")
        self.assertEqual(result.status, FAILED)
        self.assertIn("no hub commit matches", result.detail)

    def test_a_commit_with_no_hub_file_trailer_is_refused(self):
        seed = git("-C", str(self.hub), "rev-parse", "HEAD").strip()
        result = self.reject(seed, reason="not mine")
        self.assertEqual(result.status, FAILED)
        self.assertIn("names no hub file", result.detail)

    def test_an_unknown_disposition_is_refused(self):
        sha = self.append("a fact")
        result = dispose(self.hub, Decision(sha, "maybe"), now=NOW, push=False)
        self.assertEqual(result.status, FAILED)
        self.assertIn("unknown disposition", result.detail)


# --- the notes ref ---------------------------------------------------------------------


class TestNotesRefIntegrity(ReviewRepoCase):
    def test_a_missing_notes_ref_means_nothing_disposed_not_everything_approved(self):
        sha = self.append("a fact")
        self.assertEqual(disposed_shas(self.hub), set())
        self.assertEqual([c.sha for c in self.pending()], [sha])

    def test_an_unreadable_notes_ref_fails_loudly(self):
        self.append("a fact")
        self.approve(git("-C", str(self.hub), "rev-parse", "HEAD").strip())
        ref = self.hub / ".git" / "refs" / "notes" / "buzai-review"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text("0" * 39 + "1\n")  # points at an object that is not there

        with self.assertRaises(HubReviewError) as cm:
            disposed_shas(self.hub)
        self.assertIn(NOTES_REF, str(cm.exception))
        with self.assertRaises(HubReviewError):
            run_list(self.hub, 10, NOW)

    def test_a_corrupt_note_is_never_read_as_a_verdict(self):
        sha = self.append("a fact")
        git(
            "-C",
            str(self.hub),
            "notes",
            f"--ref={NOTES_REF}",
            "add",
            "-m",
            "this is not json",
            sha,
        )
        with self.assertRaises(HubReviewError):
            read_disposition(self.hub, sha)


class TestNotesPushEnvironmentIsolation(ReviewRepoCase):
    """The notes push is the fourth network-facing call, and gets the same layer 1.

    Dispositions are the owner's words about their own knowledge base, so a redirected
    notes push leaks exactly what a redirected content push leaks. The overlay asserted
    here is the one `push_notes` handed its runner.
    """

    def setUp(self):
        super().setUp()
        self.approve(self.append("a fact"))
        plant(self)
        self.calls = []

        def recorder(args, env, timeout):
            self.calls.append((list(args), dict(env), timeout))
            # The push itself is never executed: nothing here may leave the box.
            return GitResult(0, "", "") if "push" in args else real_git(args, env, timeout)

        outcome = push_notes(self.hub, runner=recorder, verifier=PRIVATE_REMOTE)
        self.assertEqual(outcome.status, PUSHED, outcome.detail)
        pushes = [call for call in self.calls if "push" in call[0]]
        self.assertEqual(len(pushes), 1)
        self.args, self.env, _ = pushes[0]

    def test_nothing_injected_reaches_the_notes_pushs_child(self):
        assert_injection_suppressed(self, self.env)

    def test_the_notes_push_resets_the_proxy_on_the_command_line(self):
        overrides = [self.args[i + 1] for i, a in enumerate(self.args) if a == "-c"]
        self.assertIn("http.proxy=", overrides)

    def test_the_notes_push_keeps_the_credential_channels_it_needs(self):
        for name in ("SSH_AUTH_SOCK", "GIT_ASKPASS", "GIT_CONFIG_GLOBAL"):
            self.assertNotIn(name, self.env, name)
        self.assertEqual(self.env["GIT_TERMINAL_PROMPT"], "0")


class TestNotesPush(ReviewRepoCase):
    def setUp(self):
        super().setUp()
        self.sha = self.append("a fact")
        self.remote = self.bare_remote()

    def notes_on_remote(self) -> str:
        return git("-C", str(self.remote), "for-each-ref", "--format=%(refname)", "refs/notes")

    def test_nothing_to_push_before_any_verdict(self):
        outcome = push_notes(self.hub, verifier=local_verifier(self.remote))
        self.assertEqual(outcome.status, NOTHING_TO_PUSH)

    def test_a_verified_private_remote_receives_the_dispositions(self):
        self.approve(self.sha)
        outcome = push_notes(self.hub, verifier=local_verifier(self.remote))
        self.assertEqual(outcome.status, PUSHED, outcome.detail)
        self.assertIn(NOTES_REF, self.notes_on_remote())

    def test_an_unverified_remote_gets_nothing(self):
        self.approve(self.sha)
        outcome = push_notes(self.hub, verifier=UNVERIFIED_REMOTE)
        self.assertEqual(outcome.status, PUSH_REFUSED)
        self.assertEqual(self.notes_on_remote().strip(), "")

    def test_a_local_only_hub_keeps_its_dispositions_without_failing(self):
        self.approve(self.sha)
        outcome = push_notes(self.hub, verifier=NO_REMOTE)
        self.assertEqual(outcome.status, PUSH_LOCAL_ONLY)

    def test_a_diverged_notes_ref_halts_instead_of_merging(self):
        self.approve(self.sha)
        git("-C", str(self.hub), "push", "-q", "origin", "HEAD:refs/heads/main")
        push_notes(self.hub, verifier=local_verifier(self.remote))
        # another instance disposed something else and pushed it first
        other = self.root / "other"
        git("clone", "-q", str(self.remote), str(other))
        git("-C", str(other), "fetch", "-q", "origin", f"{NOTES_REF}:{NOTES_REF}")
        git("-C", str(other), "notes", f"--ref={NOTES_REF}", "add", "-m", "{}", "HEAD~1")
        git("-C", str(other), "push", "-q", "origin", f"{NOTES_REF}:{NOTES_REF}")
        remote_notes = git("-C", str(self.remote), "rev-parse", NOTES_REF).strip()

        second = self.append("another fact")
        self.approve(second)
        outcome = push_notes(self.hub, verifier=local_verifier(self.remote))

        self.assertEqual(outcome.status, DIVERGED)
        self.assertIn("HALTED", outcome.detail)
        self.assertEqual(git("-C", str(self.remote), "rev-parse", NOTES_REF).strip(), remote_notes)

    def test_a_disposition_pushes_its_notes_with_it(self):
        result = self.approve(self.sha, push=True, verifier=local_verifier(self.remote))
        self.assertEqual(result.notes.status, PUSHED, result.notes.detail)
        self.assertIn(NOTES_REF, self.notes_on_remote())

    def test_dispositions_survive_a_restore_from_the_remote(self):
        """R8: a fresh instance fetches the notes ref and does not re-review history."""
        self.approve(self.sha, push=True, verifier=local_verifier(self.remote))
        restored = self.root / "restored"
        git("clone", "-q", str(self.remote), str(restored))
        self.assertEqual(disposed_shas(restored), set())  # clone does not fetch notes
        git("-C", str(restored), "fetch", "-q", "origin", "refs/notes/*:refs/notes/*")
        self.assertEqual(disposed_shas(restored), {self.sha})


# --- concurrency -------------------------------------------------------------------------


class TestConcurrentDisposals(ReviewRepoCase):
    """Two sessions disposing at the same time. Per-commit notes cannot conflict, but the
    ref update behind them can — so both verdicts must land, and neither correction may be
    applied twice."""

    def test_two_sessions_dispose_at_once_and_both_land(self):
        facts = ["fact-A", "fact-B", "fact-C"]
        shas = {fact: self.append(fact) for fact in facts}
        driver = self.root / "driver.py"
        driver.write_text(DRIVER.format(repo=str(REPO_ROOT)))
        start_at = time.time() + 1.5

        procs = [
            subprocess.Popen(
                [sys.executable, str(driver), str(self.hub), shas[fact], fact, str(start_at)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for fact in ("fact-A", "fact-C")
        ]
        outs = [(p.wait(timeout=90), p.communicate()) for p in procs]

        self.assertEqual([rc for rc, _ in outs], [0, 0], outs)
        content = self.notes.read_text()
        self.assertNotIn("fact-A", content)
        self.assertNotIn("fact-C", content)
        self.assertIn("- fact-B", content)  # the item nobody judged is untouched

        disposed = disposed_shas(self.hub)
        self.assertIn(shas["fact-A"], disposed)
        self.assertIn(shas["fact-C"], disposed)
        self.assertNotIn(shas["fact-B"], disposed)
        self.assertEqual([c.sha for c in self.pending()], [shas["fact-B"]])

    def test_simultaneous_verdicts_are_never_silently_lost(self):
        """The one that fails against a naive implementation. Approvals do not touch a
        hub file, so nothing but this module's lock serializes them — and concurrent
        `git notes add` calls each rebuild the notes tree from the same parent, keep the
        last writer, and *both* exit 0. Verified: with the lock removed, four overlapping
        approvals leave two dispositions and every process reports success."""
        shas = [self.append(f"fact-{n}") for n in range(4)]
        driver = self.root / "approve.py"
        driver.write_text(APPROVE_DRIVER.format(repo=str(REPO_ROOT)))
        start_at = time.time() + 1.5

        procs = [
            subprocess.Popen(
                [sys.executable, str(driver), str(self.hub), sha, str(start_at)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for sha in shas
        ]
        outs = [(p.wait(timeout=90), p.communicate()) for p in procs]

        self.assertEqual([rc for rc, _ in outs], [0] * len(shas), outs)
        self.assertEqual(disposed_shas(self.hub), set(shas))
        self.assertEqual(self.pending(), [])

    def test_no_correction_is_applied_twice(self):
        sha = self.append("fact-A")
        first = self.reject(sha, reason="wrong")
        second = self.reject(sha, reason="wrong again")
        self.assertEqual(first.status, RECORDED)
        self.assertEqual(second.status, ALREADY_DISPOSED)
        removals = [s for s in self.subjects() if s.startswith("Remove content the owner rejected")]
        self.assertEqual(len(removals), 1)


# --- the verb ------------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def test_a_refused_hub_path_exits_1_before_reading_anything(self):
        self.addCleanup(restore_env, "BUZAI_HUBS_DIR", os.environ.get("BUZAI_HUBS_DIR"))
        os.environ["BUZAI_HUBS_DIR"] = str(REPO_ROOT)  # inside the public checkout
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main([])
        self.assertEqual(rc, 1)
        self.assertIn("hub-review FAIL", err.getvalue())

    def test_an_uninitialized_hub_is_reported_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.addCleanup(restore_env, "BUZAI_HUBS_DIR", os.environ.get("BUZAI_HUBS_DIR"))
            os.environ["BUZAI_HUBS_DIR"] = str(Path(tmp) / "hubs")
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                rc = main([])
            self.assertEqual(rc, 1)
            self.assertIn("make hub-init", err.getvalue())
            self.assertFalse((Path(tmp) / "hubs").exists())


class TestMainAgainstARealHub(ReviewRepoCase):
    """`main` against a hub with no remote — `hub_remote.verify` answers local-only
    without probing anything, so nothing here can reach the network."""

    def setUp(self):
        super().setUp()
        self.addCleanup(restore_env, "BUZAI_HUBS_DIR", os.environ.get("BUZAI_HUBS_DIR"))
        os.environ["BUZAI_HUBS_DIR"] = str(self.hub)
        self.sha = self.append("the dentist is on Oak Street")

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_the_default_action_lists_what_is_pending(self):
        rc, out, _ = self.run_main([])
        self.assertEqual(rc, 0)
        self.assertIn("hubs resolve to", out)
        self.assertIn("1 change(s) awaiting review", out)
        self.assertIn("Record the dentist is on Oak Street", out)

    def test_approving_from_the_command_line_settles_the_item(self):
        rc, out, _ = self.run_main(["--approve", self.sha[:8], "--no-push"])
        self.assertEqual(rc, 0)
        self.assertIn("approved", out)
        _, out, _ = self.run_main([])
        self.assertIn("nothing is awaiting your review", out)

    def test_rejecting_without_a_reason_exits_1(self):
        rc, _, err = self.run_main(["--reject", self.sha, "--no-push"])
        self.assertEqual(rc, 1)
        self.assertIn("must carry the owner's reason", err)
        self.assertIn("Oak Street", self.notes.read_text())

    def test_rejecting_from_the_command_line_removes_the_content(self):
        rc, out, _ = self.run_main(["--reject", self.sha, "--reason", "wrong street", "--no-push"])
        self.assertEqual(rc, 0)
        self.assertIn("correction committed as", out)
        self.assertNotIn("Oak Street", self.notes.read_text())

    def test_push_notes_on_a_local_only_hub_exits_0(self):
        self.approve(self.sha)
        rc, out, err = self.run_main(["--push-notes"])
        self.assertEqual(rc, 0)
        self.assertIn("no remote is configured", err + out)

    def test_a_stale_backlog_leads_the_listing(self):
        record_unpushed(
            default_backlog_path(self.hub), self.sha, NOW - timedelta(days=3), "offline earlier"
        )
        _, out, _ = self.run_main([])
        warning = [line for line in out.splitlines() if "WARNING" in line]
        self.assertTrue(warning, out)
        self.assertIn("have not reached the private remote", warning[0])


if __name__ == "__main__":
    unittest.main()
