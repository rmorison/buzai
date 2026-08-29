import io
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout, suppress
from datetime import UTC, datetime
from pathlib import Path

from scripts.hub_init import (
    BOOTSTRAP_SOURCE,
    MARKER,
    SCAFFOLDS,
    TIMEOUT_RETURNCODE,
    HubInitError,
    commit_count,
    commit_message,
    migration_message,
    detail_of,
    git_ok,
    initialize,
    main,
    owner_instructions,
    parse_marker,
    plan_migration,
    plan_seed,
    remotes,
    render_marker,
    resume_message,
    tracked_paths,
)
from scripts.hub_init import git as git_call

NOW = datetime(2026, 8, 5, 12, 30, 0, tzinfo=UTC)
NO_GIT = "buzai-definitely-not-a-git-binary"

# Hermetic git: no global/system config reaches these tests (a machine with none must
# pass, and a maintainer's gpgsign/hooks/defaultBranch settings must not leak in), and
# the identity commits need comes from the environment instead.
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


def restore_mode(path: Path, mode: int = 0o700) -> None:
    with suppress(OSError):
        path.chmod(mode)


def git(*args, cwd=None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout


def make_checkout(root: Path) -> Path:
    """A stand-in public checkout with the two tracked scaffolds. Returns its `hubs/`."""
    hubs = root / "hubs"
    hubs.mkdir(parents=True)
    (hubs / "README.md").write_text("# Hubs — scaffolds live here, your content does not\n")
    (hubs / "_example-hub.md").write_text("# <Domain> hub\n")
    git("init", "-q", "-b", "main", str(root))
    git("-C", str(root), "add", "-A")
    git("-C", str(root), "commit", "-q", "-m", "scaffolds")
    return hubs


class HubTempCase(unittest.TestCase):
    """Everything runs in a tempdir: never the real ~/hubs and never the real checkout."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.checkout = self.root / "checkout"
        self.source = make_checkout(self.checkout)
        self.hub = self.root / "hubs"

    def tearDown(self):
        self._tmp.cleanup()

    def tracked(self) -> set[str]:
        return tracked_paths(self.source)

    def indexed(self) -> set[str]:
        """What the public checkout's git index holds, repo-relative."""
        return set(git("-C", str(self.checkout), "ls-files").split())

    def track(self, rel: str, body: str, commit: bool = True) -> Path:
        """PLANT: a personal hub file the public checkout already tracks."""
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        git("-C", str(self.checkout), "add", "--", f"hubs/{rel}")
        if commit:
            git("-C", str(self.checkout), "commit", "-q", "-m", f"track {rel}")
        return path

    def init(self, hub=None, now=NOW, git_bin="git", scaffolds=SCAFFOLDS):
        return initialize(hub or self.hub, self.source, now, git_bin, scaffolds)

    def committed(self, hub=None) -> set[str]:
        out = git("-C", str(hub or self.hub), "ls-tree", "-r", "--name-only", "HEAD")
        return set(out.split())

    def commits(self, hub=None) -> int:
        return int(git("-C", str(hub or self.hub), "rev-list", "--count", "HEAD").strip())


class TestScaffoldSet(unittest.TestCase):
    """The scaffold set is named, not inferred — the whole point of the fix."""

    def test_exactly_the_two_files_the_public_repo_ships(self):
        self.assertEqual(set(SCAFFOLDS), {"README.md", "_example-hub.md"})


class TestPlanSeed(unittest.TestCase):
    def test_public_readme_is_not_seeded(self):
        # the hub store gets its own README; the public one describes the template dir
        self.assertEqual(plan_seed(), ["_example-hub.md"])

    def test_a_new_scaffold_is_seeded_from_the_named_set(self):
        self.assertEqual(
            plan_seed({"README.md", "_example-hub.md", "CONVENTIONS.md"}),
            ["CONVENTIONS.md", "_example-hub.md"],
        )


class TestPlanMigration(HubTempCase):
    def test_scaffolds_are_not_migrated(self):
        self.assertEqual(plan_migration(self.source), [])

    def test_untracked_files_are_migrated_including_folder_hubs(self):
        (self.source / "finance-and-tax.md").write_text("real content\n")
        (self.source / "trip").mkdir()
        (self.source / "trip" / "notes.md").write_text("more real content\n")
        self.assertEqual(
            plan_migration(self.source),
            ["finance-and-tax.md", "trip/notes.md"],
        )

    def test_tracked_personal_content_is_migrated_not_exempted(self):
        # PLANT: the hole. A committed hubs/personal.md used to read as "scaffold" —
        # so the file sitting in the PUBLIC repo was the one thing migration skipped.
        self.track("personal.md", "# Personal\n")
        self.assertIn("personal.md", plan_migration(self.source))

    def test_tracked_content_in_a_subdirectory_is_migrated_too(self):
        self.track("trip/notes.md", "# Trip\n")
        self.assertIn("trip/notes.md", plan_migration(self.source))

    def test_missing_source_directory_is_not_an_error(self):
        self.assertEqual(plan_migration(self.root / "absent"), [])


class TestTrackedPaths(HubTempCase):
    def test_reports_what_git_tracks_including_personal_content(self):
        # tracked is NOT scaffold: this answers "what does the public repo already
        # carry", which only chooses the remedy
        (self.source / "untracked.md").write_text("real content\n")
        self.track("personal.md", "# Personal\n")
        self.assertEqual(self.tracked(), {"README.md", "_example-hub.md", "personal.md"})

    def test_outside_a_checkout_fails_loudly(self):
        # "git could not say" must never be reported as "the repo carries nothing"
        loose = self.root / "loose"
        loose.mkdir()
        with self.assertRaises(HubInitError):
            tracked_paths(loose)


class TestMarker(unittest.TestCase):
    def test_roundtrips_through_comments(self):
        self.assertEqual(parse_marker(render_marker(NOW)), NOW)

    def test_timestamp_carries_an_explicit_offset(self):
        self.assertIsNotNone(parse_marker(render_marker(NOW)).tzinfo)

    def test_unparseable_marker_raises(self):
        for bad in ("", "# only comments\n", "not-a-timestamp\n"):
            with self.assertRaises(ValueError):
                parse_marker(bad)


class TestInitializeHappyPath(HubTempCase):
    def setUp(self):
        super().setUp()
        self.result = self.init()

    def test_reports_creation(self):
        self.assertEqual(self.result.status, "created")
        self.assertEqual(self.result.seeded, ["_example-hub.md"])
        self.assertEqual(self.result.remotes, [])

    def test_directory_and_repo_exist(self):
        self.assertTrue((self.hub / ".git").exists())
        self.assertEqual(
            git("-C", str(self.hub), "rev-parse", "--abbrev-ref", "HEAD").strip(), "main"
        )

    def test_scaffold_seeded_and_instance_readme_written(self):
        self.assertTrue((self.hub / "_example-hub.md").exists())
        readme = (self.hub / "README.md").read_text()
        self.assertIn("Restore onto a fresh instance", readme)
        self.assertNotIn("your content does not", readme)  # not the public scaffold README

    def test_marker_records_the_creation_timestamp(self):
        self.assertEqual(parse_marker((self.hub / MARKER).read_text()), NOW)

    def test_exactly_one_commit_containing_everything(self):
        self.assertEqual(self.commits(), 1)
        self.assertEqual(self.result.commits, 1)
        self.assertEqual(self.committed(), {"README.md", "_example-hub.md", MARKER.as_posix()})

    def test_working_tree_is_clean(self):
        self.assertEqual(git("-C", str(self.hub), "status", "--porcelain"), "")

    def test_commit_message_carries_the_instruction_source(self):
        body = git("-C", str(self.hub), "log", "-1", "--pretty=%B")
        self.assertIn("Initialize hub store", body)
        self.assertIn(f"instruction-source: {BOOTSTRAP_SOURCE}", body)


class TestSecondRun(HubTempCase):
    def test_existing_repo_is_reported_and_left_untouched(self):
        self.init()
        (self.hub / "_example-hub.md").unlink()  # deleted on purpose: must NOT be re-seeded
        (self.hub / "finance-and-tax.md").write_text("owner content\n")

        result = self.init()

        self.assertEqual(result.status, "exists")
        self.assertEqual(result.commits, 1)
        self.assertEqual(result.seeded, [])
        self.assertEqual(result.migrated, [])
        self.assertFalse((self.hub / "_example-hub.md").exists())
        self.assertEqual((self.hub / "finance-and-tax.md").read_text(), "owner content\n")
        self.assertEqual(self.commits(), 1)

    def test_second_run_over_a_clean_checkout_changes_nothing(self):
        (self.source / "finance-and-tax.md").write_text("real content\n")
        self.init()

        result = self.init()

        self.assertEqual(result.status, "exists")
        self.assertEqual(result.migrated, [])
        # 2 = the seed plus the one migration commit the FIRST run made; the point is
        # that this second run added no third, empty one.
        self.assertEqual(self.commits(), 2)

    def test_repo_with_a_remote_reports_it(self):
        self.init()
        git("-C", str(self.hub), "remote", "add", "origin", "git@example.invalid:owner/hubs.git")
        self.assertEqual(self.init().remotes, ["origin"])


class TestMigration(HubTempCase):
    def setUp(self):
        super().setUp()
        (self.source / "finance-and-tax.md").write_text("real hub content\n")
        (self.source / "trip").mkdir()
        (self.source / "trip" / "notes.md").write_text("folder hub content\n")
        self.result = self.init()

    def test_content_moves_into_the_hub_repo(self):
        self.assertEqual(self.result.migrated, ["finance-and-tax.md", "trip/notes.md"])
        self.assertEqual((self.hub / "finance-and-tax.md").read_text(), "real hub content\n")
        self.assertEqual((self.hub / "trip" / "notes.md").read_text(), "folder hub content\n")

    def test_checkout_keeps_only_the_tracked_scaffolds(self):
        self.assertEqual(
            {p.relative_to(self.source).as_posix() for p in self.source.rglob("*") if p.is_file()},
            {"README.md", "_example-hub.md"},
        )
        self.assertFalse((self.source / "trip").exists())  # emptied directory pruned

    def root_sha(self) -> str:
        return git("-C", str(self.hub), "rev-list", "--max-parents=0", "HEAD").strip()

    def test_migrated_content_is_committed(self):
        self.assertIn("finance-and-tax.md", self.committed())
        self.assertIn("trip/notes.md", self.committed())

    def test_migration_is_a_child_commit_not_the_parentless_seed(self):
        # This test used to assert commits() == 1, which encoded the bug: hub_review
        # excludes the parentless commit, so migrated content folded into the seed was
        # invisible to review forever.
        self.assertEqual(self.commits(), 2)
        roots = git("-C", str(self.hub), "rev-list", "--max-parents=0", "HEAD").split()
        self.assertEqual(roots, [self.root_sha()])
        # HEAD is the migration, and its parent is the seed — the shape hub_review needs.
        head = git("-C", str(self.hub), "rev-parse", "HEAD").strip()
        self.assertNotEqual(head, self.root_sha())
        self.assertEqual(git("-C", str(self.hub), "rev-parse", "HEAD^").strip(), self.root_sha())

    def test_the_seed_commit_holds_no_migrated_content(self):
        seeded = git("-C", str(self.hub), "ls-tree", "-r", "--name-only", self.root_sha())
        self.assertNotIn("finance-and-tax.md", seeded)
        self.assertNotIn("trip/notes.md", seeded)
        self.assertIn("_example-hub.md", seeded)

    def test_the_migration_commit_is_reviewable_by_source(self):
        body = git("-C", str(self.hub), "log", "-1", "--pretty=%B")
        self.assertIn("instruction-source: owner-directed", body)
        self.assertNotIn(BOOTSTRAP_SOURCE, body)

    def test_commit_message_names_what_moved(self):
        body = git("-C", str(self.hub), "log", "-1", "--pretty=%B")
        self.assertIn("finance-and-tax.md", body)


class TestCommittedContentMigration(HubTempCase):
    """PLANT: personal hub content the public repo has already COMMITTED under hubs/.

    The exempt-because-tracked bug lived exactly here, and deleting the file from disk
    would not have been enough: the index entry is what the public repo pushes.
    """

    def setUp(self):
        super().setUp()
        self.leak = self.track("personal.md", "# Personal\n")
        self.result = self.init()

    def test_it_is_migrated_into_the_hub_repo(self):
        self.assertIn("personal.md", self.result.migrated)
        self.assertEqual((self.hub / "personal.md").read_text(), "# Personal\n")
        self.assertIn("personal.md", self.committed())

    def test_it_is_gone_from_the_public_working_tree(self):
        self.assertFalse(self.leak.exists())

    def test_it_is_gone_from_the_public_index(self):
        self.assertNotIn("hubs/personal.md", self.indexed())

    def test_the_scaffolds_are_left_alone_in_both(self):
        self.assertEqual(
            {p.relative_to(self.source).as_posix() for p in self.source.rglob("*") if p.is_file()},
            {"README.md", "_example-hub.md"},
        )
        self.assertEqual(
            self.indexed() & {"hubs/README.md", "hubs/_example-hub.md"},
            {"hubs/README.md", "hubs/_example-hub.md"},
        )


class TestStagedContentMigration(HubTempCase):
    """`git add`ed but never committed — the case `git rm --cached` refuses without -f."""

    def setUp(self):
        super().setUp()
        self.leak = self.track("personal.md", "# Personal\n", commit=False)
        self.result = self.init()

    def test_it_is_migrated_and_de_indexed(self):
        self.assertIn("personal.md", self.result.migrated)
        self.assertEqual((self.hub / "personal.md").read_text(), "# Personal\n")
        self.assertFalse(self.leak.exists())
        self.assertNotIn("hubs/personal.md", self.indexed())


class TestResumeInterruptedMigration(HubTempCase):
    """PLANT: a hub repo that exists while personal content is STILL in the checkout.

    This is what `make hub-init` interrupted mid-migration leaves behind, and it used to
    be inescapable: the preflight fataled on the content (correctly — it is a leak) and
    blocked service start, while a re-run saw the repo, reported "exists" and stopped.
    Five starts later `StartLimitBurst=5` leaves the unit failed for good.
    """

    def setUp(self):
        super().setUp()
        self.init()  # the interrupted run got this far
        self.leak = self.source / "finance-and-tax.md"
        self.leak.write_text("real hub content\n")  # ...and no further
        self.result = self.init()

    def test_it_resumes_rather_than_reporting_exists(self):
        self.assertEqual(self.result.status, "resumed")
        self.assertEqual(self.result.migrated, ["finance-and-tax.md"])

    def test_the_content_reaches_the_hub_repo(self):
        self.assertEqual((self.hub / "finance-and-tax.md").read_text(), "real hub content\n")

    def test_the_leak_is_gone_from_the_checkout(self):
        self.assertFalse(self.leak.exists())
        self.assertEqual(
            {p.relative_to(self.source).as_posix() for p in self.source.rglob("*") if p.is_file()},
            {"README.md", "_example-hub.md"},
        )

    def test_the_content_is_committed_not_merely_copied(self):
        self.assertEqual(self.commits(), 2)
        self.assertIn("finance-and-tax.md", self.committed())

    def test_the_commit_says_what_it_was_and_who_asked(self):
        body = git("-C", str(self.hub), "log", "-1", "--pretty=%B")
        self.assertIn("interrupted", body)
        self.assertIn("finance-and-tax.md", body)
        self.assertIn("instruction-source: owner-directed", body)

    def test_a_third_run_reports_exists_and_changes_nothing(self):
        again = self.init()
        self.assertEqual(again.status, "exists")
        self.assertEqual(self.commits(), 2)

    def test_the_seeded_scaffold_is_never_re_seeded_by_a_resume(self):
        self.assertEqual(self.result.seeded, [])


class TestResumeAfterInterruptedRemoval(HubTempCase):
    """PLANT: interrupted one step later — the copy is committed, the original remains."""

    def setUp(self):
        super().setUp()
        (self.source / "finance-and-tax.md").write_text("real hub content\n")
        self.init()
        self.leak = self.source / "finance-and-tax.md"
        self.leak.write_text("real hub content\n")  # the removal never happened

    def test_the_identical_copy_is_not_committed_twice(self):
        result = self.init()
        self.assertEqual(result.status, "resumed")
        self.assertEqual(self.commits(), 2)  # seed + the first run's migration, nothing new
        self.assertFalse(self.leak.exists())

    def test_a_differing_copy_is_refused_and_nothing_is_deleted(self):
        self.leak.write_text("a DIFFERENT version of the owner's content\n")
        with self.assertRaises(HubInitError) as raised:
            self.init()
        self.assertIn("differs", str(raised.exception))
        self.assertTrue(self.leak.exists())
        self.assertEqual((self.hub / "finance-and-tax.md").read_text(), "real hub content\n")


class TestResumeTakesTrackedContentOutOfTheIndex(HubTempCase):
    """The worst version of the interrupted state: the public repo already tracks it."""

    def setUp(self):
        super().setUp()
        self.init()
        self.leak = self.track("personal.md", "# Personal\n")
        self.result = self.init()

    def test_it_is_migrated_de_indexed_and_deleted(self):
        self.assertEqual(self.result.status, "resumed")
        self.assertEqual((self.hub / "personal.md").read_text(), "# Personal\n")
        self.assertFalse(self.leak.exists())
        self.assertNotIn("hubs/personal.md", self.indexed())


class TestGitCallsAreBounded(HubTempCase):
    """PLANT: a git that never returns — the ExecStartPre hang that ends in a failed unit.

    `secrets_preflight` reads commit counts, remotes and the checkout's index through
    these functions on every service start, over paths that can be network mounts. What
    each does with a timeout is the point: durability readers must not report a
    reassuring value, and the leak reader must not report "nothing tracked".
    """

    def setUp(self):
        super().setUp()
        self.init()
        self.slow = self.root / "slow-git"
        self.slow.write_text("#!/bin/sh\nsleep 2\n")
        self.slow.chmod(0o755)

    def test_a_hung_git_is_killed_and_reported_as_such(self):
        started = time.monotonic()
        result = git_call(["--version"], str(self.slow), 0.3)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(result.returncode, TIMEOUT_RETURNCODE)
        self.assertIn("timed out", result.stderr)

    def test_commit_count_never_reports_zero_commits_for_a_hung_git(self):
        # 0 is a real answer — "no history to lose yet" — and silences the durability
        # warning, so a timeout must not be able to impersonate it
        with self.assertRaises(HubInitError) as raised:
            commit_count(self.hub, str(self.slow), 0.3)
        self.assertIn("timed out", str(raised.exception))

    def test_remotes_never_reports_no_remote_for_a_hung_git(self):
        with self.assertRaises(HubInitError):
            remotes(self.hub, str(self.slow), 0.3)

    def test_tracked_paths_never_reports_nothing_tracked_for_a_hung_git(self):
        with self.assertRaises(HubInitError) as raised:
            tracked_paths(self.source, str(self.slow), 0.3)
        self.assertIn("timed out", str(raised.exception))


class TestRefusals(HubTempCase):
    def test_non_empty_non_repo_directory_is_refused(self):
        self.hub.mkdir()
        (self.hub / "something.md").write_text("pre-existing\n")

        with self.assertRaises(HubInitError) as cm:
            self.init()

        self.assertIn(str(self.hub), str(cm.exception))
        self.assertFalse((self.hub / ".git").exists())  # never initialized over
        self.assertEqual((self.hub / "something.md").read_text(), "pre-existing\n")

    def test_existing_empty_directory_is_usable(self):
        self.hub.mkdir()
        self.assertEqual(self.init().status, "created")
        self.assertTrue((self.hub / ".git").exists())

    def test_target_that_is_a_file_is_refused(self):
        self.hub.write_text("not a directory\n")
        with self.assertRaises(HubInitError) as cm:
            self.init()
        self.assertIn(str(self.hub), str(cm.exception))


class TestErrorPaths(HubTempCase):
    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_target_fails_cleanly_without_a_partial_repo(self):
        parent = self.root / "read-only"
        parent.mkdir()
        parent.chmod(0o500)
        # tolerant: cleanups run after tearDown has already removed the tempdir
        self.addCleanup(restore_mode, parent)
        target = parent / "hubs"

        with self.assertRaises(HubInitError) as cm:
            self.init(hub=target)

        self.assertIn(str(target), str(cm.exception))
        self.assertFalse(target.exists())

    def test_missing_git_fails_cleanly_without_a_partial_repo(self):
        with self.assertRaises(HubInitError) as cm:
            self.init(git_bin=NO_GIT)

        self.assertIn(NO_GIT, str(cm.exception))
        self.assertFalse(self.hub.exists())

    def test_failure_after_git_init_rolls_the_repo_back(self):
        # a named scaffold that is missing from the working tree
        (self.source / "_example-hub.md").unlink()

        with self.assertRaises(HubInitError) as cm:
            self.init()

        self.assertIn("_example-hub.md", str(cm.exception))
        self.assertFalse(self.hub.exists())

    def test_rollback_keeps_an_existing_empty_directory_and_the_checkout_intact(self):
        self.hub.mkdir()
        (self.source / "finance-and-tax.md").write_text("real hub content\n")
        (self.source / "_example-hub.md").unlink()

        with self.assertRaises(HubInitError):
            self.init()

        self.assertEqual(list(self.hub.iterdir()), [])  # nothing of ours left behind
        # the migration source is only deleted after a successful commit
        self.assertEqual((self.source / "finance-and-tax.md").read_text(), "real hub content\n")


class TestOwnerInstructions(unittest.TestCase):
    TEXT = owner_instructions(Path("/home/owner/hubs"))

    def test_names_the_hub_repo_and_the_exact_commands(self):
        self.assertIn("/home/owner/hubs", self.TEXT)
        self.assertIn("--private", self.TEXT)
        self.assertIn("remote add origin", self.TEXT)
        self.assertIn("push -u origin main", self.TEXT)

    def test_says_the_owner_runs_it(self):
        self.assertIn("buzai will not do it for you", self.TEXT)


class TestCommitMessage(unittest.TestCase):
    def test_the_seed_message_never_mentions_migration(self):
        # The seed commit carries scaffolding only; migration is its own commit.
        self.assertNotIn("moved", commit_message().casefold())

    def test_migration_is_listed_in_its_own_message(self):
        message = migration_message(["finance-and-tax.md"])
        self.assertIn("1 hub file(s)", message)
        self.assertIn("finance-and-tax.md", message)


class TestMarkerRejectsANaiveTimestamp(unittest.TestCase):
    """PLANT: a marker file whose timestamp carries no UTC offset.

    The documented format requires one and `render_marker` always writes one, but this is
    plain text inside the owner's own repo — hand-edited, restored from an older format,
    or written by a future code path. Its only consumer subtracts it from an aware `now`
    inside `secrets_preflight.stale_remote_warning`, a WARNING-path check on the systemd
    `ExecStartPre` line, and naive-minus-aware raises `TypeError`. That is not among the
    exceptions `read_marker` catches, so it escaped `ExecStartPre` and blocked service
    start; with `StartLimitBurst=5` a marker typo ended the assistant.

    Verified against the unfixed version: `parse_marker` returned the naive datetime and
    `stale_remote_warning` raised `TypeError: can't subtract offset-naive and
    offset-aware datetimes`.
    """

    NAIVE = "# buzai hub store\n2026-08-05T12:30:00\n"

    def test_a_naive_timestamp_is_a_value_error(self):
        with self.assertRaises(ValueError) as caught:
            parse_marker(self.NAIVE)
        self.assertIn("no UTC offset", str(caught.exception))

    def test_it_cannot_be_subtracted_from_an_aware_now(self):
        # the failure the ValueError exists to pre-empt, stated so it cannot be forgotten
        with self.assertRaises(TypeError):
            datetime.now(UTC) - datetime.fromisoformat("2026-08-05T12:30:00")

    def test_an_offset_bearing_timestamp_is_still_accepted(self):
        # the guard must not refuse what the format actually produces
        self.assertEqual(parse_marker(render_marker(NOW)), NOW)
        self.assertEqual(parse_marker("2026-08-05T12:30:00+02:00").utcoffset().seconds, 7200)

    def test_garbage_is_still_a_value_error(self):
        with self.assertRaises(ValueError):
            parse_marker("# only comments\n")
        with self.assertRaises(ValueError):
            parse_marker("not a timestamp\n")


class TestTheSeedIsNotStampedLikeRecordedKnowledge(unittest.TestCase):
    """The seed commit's `instruction-source` says what it IS, not who ran it.

    `owner-directed` was true — the owner ran `make hub-init` — and useless: by source
    alone the store's own scaffolding was indistinguishable from knowledge the assistant
    recorded at the owner's request, which is why `hub_review` had to exclude it by
    detecting the parentless commit instead.

    The resume commit deliberately keeps `owner-directed`: it migrates the owner's REAL
    hub content out of the public checkout, and that content should be reviewable.
    """

    def test_the_seed_carries_the_bootstrap_source(self):
        self.assertIn(f"instruction-source: {BOOTSTRAP_SOURCE}", commit_message())

    def test_the_bootstrap_source_is_distinct_from_owner_directed(self):
        self.assertNotEqual(BOOTSTRAP_SOURCE, "owner-directed")
        self.assertNotIn("owner-directed", commit_message())

    def test_the_first_run_migration_commit_is_owner_directed_and_reviewable(self):
        # the same guard as the resume path below: migrating real content is not
        # bootstrapping, on the first run just as much as on a resumed one
        moved = migration_message(["finance-and-tax.md"])
        self.assertIn("instruction-source: owner-directed", moved)
        self.assertNotIn(BOOTSTRAP_SOURCE, moved)

    def test_the_resume_commit_stays_owner_directed_and_reviewable(self):
        # the guard: migrating real content is not bootstrapping, and must not be relabelled
        self.assertIn("instruction-source: owner-directed", resume_message(["finance-and-tax.md"]))
        self.assertNotIn(BOOTSTRAP_SOURCE, resume_message(["finance-and-tax.md"]))


class TestDetailOf(unittest.TestCase):
    """One copy of the git-detail expression in this module, not three inlined ones.

    It is not `hub_commit.detail_of`: `hub_commit` imports `hub_init`, so importing it
    back is a cycle, and that function is typed over `hub_remote.GitResult` while
    everything here is a `subprocess.CompletedProcess`.
    """

    def result(self, code=1, out="", err=""):
        return subprocess.CompletedProcess(["git"], code, out, err)

    def test_the_last_line_of_stderr_wins(self):
        detail = self.result(err="warning: something\nfatal: the real reason\n")
        self.assertEqual(detail_of(detail), "fatal: the real reason")

    def test_stdout_is_the_fallback(self):
        self.assertEqual(detail_of(self.result(out="said this\n")), "said this")

    def test_silence_falls_back_to_the_exit_code(self):
        self.assertEqual(detail_of(self.result(code=128)), "exit 128")

    def test_it_has_not_drifted_from_hub_commits_copy(self):
        # the obligation the docstring takes on, made checkable: two functions that must
        # produce the same sentence, over the shapes git actually emits
        from scripts.hub_commit import detail_of as commit_detail_of
        from scripts.hub_remote import GitResult

        for code, out, err in (
            (1, "", "warning: something\nfatal: the real reason\n"),
            (1, "said this\n", ""),
            (128, "", ""),
            (1, "out\n", "err\n"),
        ):
            with self.subTest(code=code, out=out, err=err):
                self.assertEqual(
                    detail_of(self.result(code, out, err)),
                    commit_detail_of(GitResult(code, out, err)),
                )

    def test_git_ok_reports_through_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HubInitError) as caught:
                git_ok(["-C", tmp, "rev-parse", "--verify", "HEAD"], "git", "reading HEAD")
        self.assertNotIn("\n", str(caught.exception))
        self.assertIn("reading HEAD failed:", str(caught.exception))


class TestMain(unittest.TestCase):
    """Only the pre-mutation refusal is exercised here — main() targets the REAL
    checkout and the real hub location, so nothing further may run under test."""

    def test_refused_hub_path_exits_1_before_touching_anything(self):
        previous = os.environ.get("BUZAI_HUBS_DIR")
        self.addCleanup(restore_env, "BUZAI_HUBS_DIR", previous)
        os.environ["BUZAI_HUBS_DIR"] = str(Path(__file__).resolve().parents[2])  # the checkout
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = main()
        self.assertEqual(rc, 1)
        self.assertIn("hub-init FAIL", err.getvalue())


if __name__ == "__main__":
    unittest.main()
