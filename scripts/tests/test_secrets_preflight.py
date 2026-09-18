import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.hub_commit import Backlog
from scripts.hub_init import initialize
from scripts.hub_remote import default_git_runner as real_git
from scripts.secrets_preflight import (
    REMOTE_GRACE_SECONDS,
    Findings,
    HubState,
    HubTarget,
    _git_tracked,
    _repo_secret_candidates,
    assess,
    backlog_warning,
    credential_warning,
    dirty_warning,
    enclosing_repo,
    env_file_problems,
    fatal_problems,
    hub_checkout_problems,
    hub_content_problems,
    inspect_hub,
    main,
    nested_repo_warning,
    notes_for,
    origin_credential_violations,
    perm_problems,
    read_credentials,
    problems,
    read_marker,
    report,
    resolve_hub_target,
    stale_remote_warning,
    tracked_problems,
    warnings_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=UTC)

# Hermetic git: a maintainer's global credential.helper / gpgsign / defaultBranch must
# not leak into these tests, and a machine with no git config at all must pass.
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
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def git(*args) -> None:
    subprocess.run(["git", *args], capture_output=True, text=True, check=True)


def make_repo(path: Path) -> Path:
    """A real, local, network-free git repo — no test here ever leaves the box."""
    path.mkdir(parents=True, exist_ok=True)
    git("-C", str(path), "init", "-q", "-b", "main")
    return path


def commit_all(path: Path, message: str = "seed") -> None:
    git("-C", str(path), "add", "-A")
    git("-C", str(path), "commit", "-q", "-m", message)


class TestSecretsPreflight(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, name, mode):
        p = self.dir / name
        p.write_text("SECRET=x")
        os.chmod(p, mode)
        return p

    def test_0600_passes(self):
        p = self._mk("email.env", 0o600)
        self.assertEqual(perm_problems([p]), [])

    def test_0644_fails(self):
        p = self._mk("email.env", 0o644)
        probs = perm_problems([p])
        self.assertEqual(len(probs), 1)
        self.assertIn("must be 0600", probs[0])

    def test_creds_0644_fails(self):
        p = self._mk(".credentials.json", 0o640)
        self.assertEqual(len(perm_problems([p])), 1)

    def test_missing_file_skipped(self):
        self.assertEqual(perm_problems([self.dir / "nope.env"]), [])

    def test_owner_exec_bit_fails(self):
        # 0o700 has no group/other access but still diverges from 0600 (exec bit)
        p = self._mk("email.env", 0o700)
        probs = perm_problems([p])
        self.assertEqual(len(probs), 1)
        self.assertIn("must be 0600", probs[0])

    def test_tracked_secret_fails(self):
        probs = tracked_problems(["deploy/env/email.env"], is_tracked=lambda p: True)
        self.assertEqual(len(probs), 1)
        self.assertIn("tracked by git", probs[0])

    def test_untracked_secret_passes(self):
        self.assertEqual(tracked_problems(["deploy/env/email.env"], is_tracked=lambda p: False), [])

    # --- managed-connector-only: zero local secrets is valid (not a failure) ---

    def test_no_local_secrets_is_ok_when_creds_clean(self):
        # managed-only adopter: no local *.env files; credentials.json at 0600 → no problems
        creds = self._mk(".credentials.json", 0o600)
        self.assertEqual(problems([], [], creds, is_tracked=lambda p: False), [])

    def test_no_local_secrets_still_checks_creds_perms(self):
        # even with zero local secrets, loose credentials.json perms must still fail
        creds = self._mk(".credentials.json", 0o644)
        probs = problems([], [], creds, is_tracked=lambda p: False)
        self.assertEqual(len(probs), 1)
        self.assertIn("must be 0600", probs[0])

    def test_problems_flags_tracked_repo_secret(self):
        creds = self._mk(".credentials.json", 0o600)
        probs = problems([], ["deploy/env/email.env"], creds, is_tracked=lambda p: True)
        self.assertTrue(any("tracked by git" in p for p in probs))


# --- characterization: the pre-U6 contract, pinned ---------------------------------
#
# This script is the systemd `ExecStartPre`, so a regression here does not fail a test
# in CI, it leaves the service `failed` (StartLimitBurst=5) until someone logs in. These
# tests pin the exact behavior the perms/tracked checks had BEFORE the structural checks
# were added, so extending the script cannot quietly change what it already refused.


class TestPermProblemsCharacterization(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, name, mode):
        p = self.dir / name
        p.write_text("SECRET=x")
        os.chmod(p, mode)
        return p

    def test_message_format_is_path_octal_mode(self):
        p = self._mk("email.env", 0o644)
        self.assertEqual(perm_problems([p]), [f"{p} is 0o644, must be 0600"])

    def test_0400_passes(self):
        # read-only-for-owner is stricter than 0600 and has always been accepted
        self.assertEqual(perm_problems([self._mk("email.env", 0o400)]), [])

    def test_group_only_bit_fails(self):
        # 0o060: no owner bits at all, but group can read — still a leak
        self.assertEqual(len(perm_problems([self._mk("email.env", 0o060)])), 1)

    def test_accepts_str_paths(self):
        p = self._mk("email.env", 0o644)
        self.assertEqual(len(perm_problems([str(p)])), 1)

    def test_reports_every_offender_in_input_order(self):
        a = self._mk("a.env", 0o644)
        b = self._mk("b.env", 0o666)
        self.assertEqual([q.split()[0] for q in perm_problems([a, b])], [str(a), str(b)])

    def test_directory_is_not_special_cased(self):
        # a 0700 directory reports, exactly as it did before: this check has never
        # distinguished files from directories, and callers only ever feed it files
        (self.dir / "sub").mkdir(mode=0o700)
        self.assertEqual(len(perm_problems([self.dir / "sub"])), 1)


class TestTrackedProblemsCharacterization(unittest.TestCase):
    def test_message_format(self):
        self.assertEqual(
            tracked_problems(["x.env"], is_tracked=lambda p: True),
            ["x.env is tracked by git (secrets must be untracked)"],
        )

    def test_is_tracked_receives_each_path_unchanged(self):
        seen = []
        tracked_problems(["a.env", Path("b.env")], is_tracked=lambda p: seen.append(p) or False)
        self.assertEqual(seen, ["a.env", Path("b.env")])

    def test_empty_input_is_clean(self):
        self.assertEqual(tracked_problems([], is_tracked=lambda p: True), [])


class TestProblemsCharacterization(unittest.TestCase):
    """`problems()` composition: perms over secrets+repo-secrets+creds, then tracked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _mk(self, name, mode):
        p = self.dir / name
        p.write_text("SECRET=x")
        os.chmod(p, mode)
        return p

    def test_perm_problems_come_before_tracked_problems(self):
        loose = self._mk("email.env", 0o644)
        creds = self._mk(".credentials.json", 0o600)
        probs = problems([loose], [], creds, is_tracked=lambda p: True)
        self.assertEqual(len(probs), 2)
        self.assertIn("must be 0600", probs[0])
        self.assertIn("tracked by git", probs[1])

    def test_creds_perms_are_checked_but_creds_tracking_is_not(self):
        # credentials.json lives outside the repo; only secret files go to is_tracked
        creds = self._mk(".credentials.json", 0o600)
        seen = []
        problems([], [], creds, is_tracked=lambda p: seen.append(p) or False)
        self.assertEqual(seen, [])

    def test_repo_secrets_are_checked_for_both_perms_and_tracking(self):
        repo_secret = self._mk("in-repo.env", 0o644)
        creds = self._mk(".credentials.json", 0o600)
        probs = problems([], [repo_secret], creds, is_tracked=lambda p: True)
        self.assertEqual(len(probs), 2)

    def test_clean_instance_has_no_problems(self):
        creds = self._mk(".credentials.json", 0o600)
        secret = self._mk("email.env", 0o600)
        self.assertEqual(problems([secret], [], creds, is_tracked=lambda p: False), [])


# --- FATAL: real env-files inside the checkout (R16/R17) ---------------------------


class TestRepoEnvFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_planted_real_env_is_found_and_is_fatal(self):
        # PLANT: exactly what R17 forbids — a real value living in deploy/env/
        real = self.dir / "email.env"
        real.write_text("IMAP_PASSWORD=hunter2\n")
        self.assertEqual(_repo_secret_candidates(self.dir), [real])
        probs = env_file_problems(_repo_secret_candidates(self.dir))
        self.assertEqual(len(probs), 1)
        self.assertIn(str(real), probs[0])

    def test_example_scaffolds_are_not_flagged(self):
        (self.dir / "email.env.example").write_text("IMAP_PASSWORD=CHANGEME\n")
        (self.dir / "xero.env.example").write_text("XERO_TOKEN=CHANGEME\n")
        self.assertEqual(_repo_secret_candidates(self.dir), [])
        self.assertEqual(env_file_problems(_repo_secret_candidates(self.dir)), [])

    def test_missing_env_dir_is_clean(self):
        self.assertEqual(_repo_secret_candidates(self.dir / "nope"), [])

    def test_real_env_is_fatal_even_when_perms_and_tracking_are_clean(self):
        # the pre-U6 checks would have passed this: 0600 and untracked. Existence alone
        # is the violation, because gitignore is not a barrier.
        real = self.dir / "email.env"
        real.write_text("X=1")
        os.chmod(real, 0o600)
        fatal = fatal_problems(
            secret_files=[],
            repo_secrets=[real],
            creds=self.dir / "absent.json",
            hub_credentials=[],
            hub_content=[],
            hub_path_problem=None,
            origin_violations=[],
            is_tracked=lambda p: False,
        )
        self.assertEqual(len(fatal), 1)
        self.assertIn("scaffolds", fatal[0])


# --- FATAL: personal hub content inside the public checkout (R3/R16) ---------------


class TestHubContentInCheckout(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name) / "checkout")
        self.hubs = self.repo / "hubs"
        self.hubs.mkdir()
        (self.hubs / "README.md").write_text("# scaffold\n")
        (self.hubs / "_example-hub.md").write_text("# example\n")
        commit_all(self.repo, "scaffolds")

    def tearDown(self):
        self._tmp.cleanup()

    def test_scaffold_only_checkout_passes(self):
        # the state every fresh clone is in — not a violation
        self.assertEqual(hub_checkout_problems(self.hubs), [])

    def test_planted_hub_markdown_fails_naming_the_file(self):
        # PLANT: real personal content in the public checkout, gitignored in the real
        # repo — which is exactly why gitignore cannot be the barrier
        leak = self.hubs / "finance-and-tax.md"
        leak.write_text("# Finance\n\n- account 1234\n")
        probs = hub_checkout_problems(self.hubs)
        self.assertEqual(len(probs), 1)
        self.assertIn(str(leak), probs[0])

    def test_planted_content_in_a_subdirectory_is_found(self):
        # a hub that outgrew one file becomes a folder; the check must recurse
        nested = self.hubs / "home" / "notes.md"
        nested.parent.mkdir()
        nested.write_text("# Home\n")
        probs = hub_checkout_problems(self.hubs)
        self.assertEqual(len(probs), 1)
        self.assertIn(str(nested), probs[0])

    def test_missing_hubs_dir_is_clean(self):
        self.assertEqual(hub_checkout_problems(self.repo / "nope"), [])

    def test_unanswerable_is_reported_not_passed(self):
        # a hubs/ directory that is not inside any git repo: git cannot say what the
        # public repo carries, and "cannot tell" must never read as "clean"
        outside = Path(self._tmp.name) / "loose-hubs"
        outside.mkdir()
        (outside / "x.md").write_text("x")
        probs = hub_checkout_problems(outside)
        self.assertTrue(any("unverified" in p for p in probs))
        self.assertTrue(any(str(outside / "x.md") in p for p in probs))

    def test_message_names_the_remedy(self):
        probs = hub_content_problems(["finance.md"], Path("/repo/hubs"), [])
        self.assertIn("/repo/hubs/finance.md", probs[0])
        self.assertIn("make hub-init", probs[0])

    def test_the_remedy_says_hub_init_resumes_an_existing_store(self):
        # the state that produces this finding is usually a half-finished migration, so
        # "run `make hub-init`" alone reads as advice the operator already followed
        for probs in (
            hub_content_problems(["finance.md"], Path("/repo/hubs"), []),
            hub_content_problems(["finance.md"], Path("/repo/hubs"), ["finance.md"]),
        ):
            self.assertIn("interrupted migration", probs[0])
            self.assertIn("even though the hub repo already exists", probs[0])

    # --- the hole: content the public repo ALREADY tracks --------------------------

    def _track(self, rel: str, body: str = "# Personal\n", commit: bool = True) -> Path:
        """PLANT: personal hub content that is already in the public repo's index."""
        path = self.hubs / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        git("-C", str(self.repo), "add", "--", f"hubs/{rel}")
        if commit:
            commit_all(self.repo, f"track {rel}")
        return path

    def test_tracked_personal_content_is_fatal_not_exempt(self):
        # the check used to read "tracked under hubs/" as "scaffold", so the file
        # sitting IN the public repo was the one thing it waved through
        leak = self._track("personal.md")
        probs = hub_checkout_problems(self.hubs)
        self.assertEqual(len(probs), 1)
        self.assertIn(str(leak), probs[0])

    def test_the_tracked_message_names_git_rm_cached(self):
        # deleting the file is not the whole remedy — the index entry is what pushes
        leak = self._track("personal.md")
        probs = hub_checkout_problems(self.hubs)
        self.assertIn(f"git rm --cached {leak}", probs[0])
        self.assertIn("PUBLIC repo already tracks", probs[0])

    def test_staged_but_uncommitted_content_is_fatal_too(self):
        leak = self._track("personal.md", commit=False)
        probs = hub_checkout_problems(self.hubs)
        self.assertEqual(len(probs), 1)
        self.assertIn(f"git rm --cached {leak}", probs[0])

    def test_tracked_content_in_a_subdirectory_is_found(self):
        leak = self._track("trip/notes.md")
        probs = hub_checkout_problems(self.hubs)
        self.assertEqual(len(probs), 1)
        self.assertIn(str(leak), probs[0])

    def test_the_untracked_message_is_kept_for_untracked_content(self):
        (self.hubs / "loose.md").write_text("# Loose\n")
        probs = hub_checkout_problems(self.hubs)
        self.assertEqual(len(probs), 1)
        self.assertIn("make hub-init", probs[0])
        self.assertNotIn("git rm --cached", probs[0])

    def test_a_tracked_leak_reaches_the_fatal_list(self):
        self._track("personal.md")
        fatal = fatal_problems(
            secret_files=[],
            repo_secrets=[],
            creds=self.repo / "absent.json",
            hub_credentials=[],
            hub_content=hub_checkout_problems(self.hubs),
            hub_path_problem=None,
            origin_violations=[],
            is_tracked=lambda p: False,
        )
        self.assertEqual(len(fatal), 1)
        self.assertIn("git rm --cached", fatal[0])


# --- FATAL: the hub path itself (R3) ----------------------------------------------


class TestResolveHubTarget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self.repo = self.home / "buzai"
        self.repo.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_default_is_safe(self):
        target = resolve_hub_target(None, self.home, self.repo)
        self.assertIsNone(target.problem)
        self.assertEqual(target.path, self.home / "hubs")
        self.assertIn("default ~/hubs", target.description)

    def test_path_inside_the_checkout_is_fatal(self):
        # PLANT: the direct leak — hub content one `git add` from the public remote
        target = resolve_hub_target(str(self.repo / "hubs"), self.home, self.repo)
        self.assertIsNotNone(target.problem)
        self.assertIn("inside the repo checkout", target.problem)

    def test_path_containing_the_checkout_is_fatal(self):
        target = resolve_hub_target(str(self.home), self.home, self.repo)
        self.assertIn("contains the repo checkout", target.problem)

    def test_symlink_into_the_checkout_is_fatal(self):
        # the failure a lexical string comparison waves through
        inside = self.repo / "hubs"
        inside.mkdir()
        link = self.home / "hubs"
        link.symlink_to(inside)
        target = resolve_hub_target(None, self.home, self.repo)
        self.assertIn("inside the repo checkout", target.problem)

    def test_unresolvable_value_classifies_instead_of_raising(self):
        # the resolver's own entry point raises; this must return a finding so the
        # remaining leak checks still run
        target = resolve_hub_target("~someone/hubs", self.home, self.repo)
        self.assertIsNone(target.path)
        self.assertIn("~user", target.problem)

    def test_env_var_source_is_reported_for_the_journal(self):
        target = resolve_hub_target(str(self.home / "elsewhere"), self.home, self.repo)
        self.assertIn("BUZAI_HUBS_DIR", target.description)


# --- FATAL: the hub push credential (R15) ------------------------------------------


class TestHubCredential(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_tracked_by_a_repo_outside_the_checkout_is_detected(self):
        # THE anti-dead-no-op test. The prior bug in this script was a tracked-check fed
        # only paths inside the checkout; the hub deploy key lives at ~/.ssh/buzai-hub,
        # so a dotfiles repo at ~ is the repo that would publish it. `git -C REPO_ROOT`
        # can never see this file — if the check regresses to that, this test fails.
        dotfiles = make_repo(self.dir / "home")
        ssh = dotfiles / ".ssh"
        ssh.mkdir()
        key = ssh / "buzai-hub"
        key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        os.chmod(key, 0o600)
        commit_all(dotfiles, "track the deploy key")

        self.assertFalse(key.is_relative_to(REPO_ROOT))
        self.assertTrue(_git_tracked(key))
        self.assertEqual(len(tracked_problems([key], _git_tracked)), 1)

    def test_untracked_key_outside_any_repo_passes(self):
        key = self.dir / "buzai-hub"
        key.write_text("k")
        os.chmod(key, 0o600)
        self.assertFalse(_git_tracked(key))

    def test_present_but_untracked_inside_a_repo_passes(self):
        dotfiles = make_repo(self.dir / "home")
        key = dotfiles / "buzai-hub"
        key.write_text("k")
        self.assertFalse(_git_tracked(key))

    def test_loose_perms_on_the_key_are_fatal(self):
        key = self.dir / "buzai-hub"
        key.write_text("k")
        os.chmod(key, 0o644)
        fatal = fatal_problems(
            secret_files=[],
            repo_secrets=[],
            creds=self.dir / "absent.json",
            hub_credentials=[key],
            hub_content=[],
            hub_path_problem=None,
            origin_violations=[],
            is_tracked=lambda p: False,
        )
        self.assertEqual(len(fatal), 1)
        self.assertIn("must be 0600", fatal[0])

    def test_absent_key_is_not_a_violation(self):
        # no hub remote configured yet is a legitimate state, not a missing credential
        absent = self.dir / "buzai-hub"
        seen = []
        fatal = fatal_problems(
            secret_files=[],
            repo_secrets=[],
            creds=self.dir / "absent.json",
            hub_credentials=[absent],
            hub_content=[],
            hub_path_problem=None,
            origin_violations=[],
            is_tracked=lambda p: seen.append(p) or True,
        )
        self.assertEqual(fatal, [])
        self.assertEqual(seen, [])  # an absent file is never asked about


# --- FATAL: a credential helper on the PUBLIC origin (R3/R4) -----------------------


class TestOriginCredentialHelper(unittest.TestCase):
    """The check itself is U3's (`hub_remote.origin_credential_violations`); what is
    proven here is that it is wired into the fatal list and fires on a planted helper."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self._tmp.name) / "checkout")
        git("-C", str(self.repo), "remote", "add", "origin", "https://example.invalid/buzai.git")

    def tearDown(self):
        self._tmp.cleanup()

    def _violations(self):
        # imported *through* secrets_preflight: the wiring is the thing under test
        return origin_credential_violations(self.repo, real_git)

    def test_clean_checkout_has_no_violation(self):
        self.assertEqual(self._violations(), [])

    def test_planted_helper_on_the_public_origin_fires(self):
        # PLANT: a helper that would hand this instance push access to the public repo —
        # the assumption the whole structural barrier rests on
        git("-C", str(self.repo), "config", "credential.helper", "store")
        violations = self._violations()
        self.assertEqual(len(violations), 1)
        self.assertIn("credential helper", violations[0])

    def test_a_planted_helper_reaches_the_fatal_list(self):
        git("-C", str(self.repo), "config", "credential.helper", "store")
        fatal = fatal_problems(
            secret_files=[],
            repo_secrets=[],
            creds=self.repo / "absent.json",
            hub_credentials=[],
            hub_content=[],
            hub_path_problem=None,
            origin_violations=self._violations(),
            is_tracked=lambda p: False,
        )
        self.assertEqual(len(fatal), 1)
        self.assertIn("credential helper", fatal[0])


# --- WARNING: durability conditions that must NOT block start ----------------------


class TestDurabilityWarnings(unittest.TestCase):
    HUB = Path("/home/someone/hubs")

    def test_no_remote_within_the_grace_period_is_quiet(self):
        state = HubState(
            exists=True, is_repo=True, commits=1, created=NOW - timedelta(hours=2), remotes=()
        )
        self.assertIsNone(stale_remote_warning(self.HUB, state, NOW))

    def test_no_remote_past_the_grace_period_warns_with_the_commit_count(self):
        state = HubState(
            exists=True, is_repo=True, commits=7, created=NOW - timedelta(days=30), remotes=()
        )
        warning = stale_remote_warning(self.HUB, state, NOW)
        self.assertIn("7 commit(s)", warning)
        self.assertIn("NO remote", warning)

    def test_a_configured_remote_silences_the_stale_warning(self):
        state = HubState(
            exists=True,
            is_repo=True,
            commits=7,
            created=NOW - timedelta(days=30),
            remotes=("origin",),
        )
        self.assertIsNone(stale_remote_warning(self.HUB, state, NOW))

    def test_zero_commits_is_not_a_durability_problem(self):
        # a store initialized but not yet written to has nothing to lose
        state = HubState(exists=True, is_repo=True, commits=0, created=NOW - timedelta(days=90))
        self.assertIsNone(stale_remote_warning(self.HUB, state, NOW))

    def test_unknown_creation_time_still_warns(self):
        state = HubState(exists=True, is_repo=True, commits=3, created=None)
        self.assertIn("creation time unknown", stale_remote_warning(self.HUB, state, NOW))

    def test_backlog_warning_reports_count_and_age(self):
        backlog = Backlog(
            pending=(("abc123", NOW - timedelta(days=3)), ("def456", NOW - timedelta(days=1))),
            last_error="deploy key rejected",
        )
        warning = backlog_warning(HubState(is_repo=True, backlog=backlog), NOW)
        self.assertIn("2 hub commit(s)", warning)
        self.assertIn("3.0 day(s)", warning)
        self.assertIn("deploy key rejected", warning)

    def test_empty_backlog_is_quiet(self):
        self.assertIsNone(backlog_warning(HubState(is_repo=True), NOW))

    def test_dirty_tree_warns_and_names_paths(self):
        state = HubState(is_repo=True, dirty=("finance-and-tax.md", "home.md"))
        self.assertIn("finance-and-tax.md", dirty_warning(self.HUB, state))

    def test_clean_tree_is_quiet(self):
        self.assertIsNone(dirty_warning(self.HUB, HubState(is_repo=True)))

    def test_nested_repo_warns(self):
        state = HubState(is_repo=True, enclosing_repo="/home/someone")
        self.assertIn("/home/someone", nested_repo_warning(self.HUB, state))

    def test_no_enclosing_repo_is_quiet(self):
        self.assertIsNone(nested_repo_warning(self.HUB, HubState(is_repo=True)))

    def test_warnings_for_collects_every_condition(self):
        state = HubState(
            exists=True,
            is_repo=True,
            commits=4,
            created=NOW - timedelta(days=60),
            backlog=Backlog(pending=(("abc", NOW - timedelta(days=2)),)),
            dirty=("home.md",),
            enclosing_repo="/home/someone",
        )
        self.assertEqual(len(warnings_for(self.HUB, state, NOW)), 4)

    def test_warnings_for_is_silent_on_a_fresh_instance(self):
        # nothing initialized yet: legitimately empty, not a violation
        self.assertEqual(warnings_for(self.HUB, HubState(), NOW), [])

    def test_unreadable_worktree_warns_rather_than_failing(self):
        state = HubState(is_repo=True, inspect_error="fatal: not a git repository")
        self.assertEqual(len(warnings_for(self.HUB, state, NOW)), 1)

    def test_garbled_marker_warns_rather_than_silently_disabling_the_check(self):
        state = HubState(is_repo=True, marker_error="marker unreadable")
        warns = warnings_for(self.HUB, state, NOW)
        self.assertTrue(any("grace period cannot be judged" in w for w in warns))

    def test_grace_period_is_a_week(self):
        self.assertEqual(REMOTE_GRACE_SECONDS, 7 * 24 * 3600.0)


# --- WARNING: proven not to block service start ------------------------------------


class TestWarningsDoNotBlockStart(unittest.TestCase):
    TARGET = HubTarget(Path("/home/someone/hubs"), "/home/someone/hubs (from default ~/hubs)")

    def _run(self, findings):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = report(self.TARGET, findings)
        return code, out.getvalue(), err.getvalue()

    def test_warnings_alone_exit_zero(self):
        # StartLimitBurst=5: a fatal durability check would leave the unit `failed` until
        # a human logs in, turning degraded knowledge into a total assistant outage
        code, out, err = self._run(Findings(warnings=("nothing has been pushed off-box",)))
        self.assertEqual(code, 0)
        self.assertIn("WARN: nothing has been pushed off-box", err)
        self.assertIn("secrets-preflight OK", out)
        self.assertIn("start not blocked", out)

    def test_every_warning_is_reported_loudly(self):
        code, _, err = self._run(Findings(warnings=("a", "b", "c")))
        self.assertEqual(code, 0)
        self.assertEqual(err.count("secrets-preflight WARN:"), 3)

    def test_a_leak_condition_blocks_start(self):
        code, out, err = self._run(Findings(fatal=("a real .env is in the checkout",)))
        self.assertEqual(code, 1)
        self.assertIn("FAIL: a real .env is in the checkout", err)
        self.assertNotIn("secrets-preflight OK", out)

    def test_warnings_are_still_reported_alongside_a_fatal(self):
        code, _, err = self._run(Findings(fatal=("leak",), warnings=("backlog",)))
        self.assertEqual(code, 1)
        self.assertIn("WARN: backlog", err)
        self.assertIn("FAIL: leak", err)

    def test_the_resolved_hub_path_is_always_printed(self):
        # a store mismatch between the unit and the owner's shell must be visible in the
        # journal, on every start, whether or not anything is wrong
        _, out, _ = self._run(Findings())
        self.assertIn("hubs resolve to /home/someone/hubs", out)

    def test_clean_instance_says_so(self):
        code, out, _ = self._run(Findings())
        self.assertEqual(code, 0)
        self.assertTrue(out.rstrip().endswith("secrets-preflight OK"))


# --- the collectors, against a real (local, network-free) hub repo -----------------


class TestInspectHub(unittest.TestCase):
    """Proves the gatherers are fed state that can actually differ — the pure warning
    functions above are only meaningful if something real reaches them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.hub = make_repo(self.root / "hubs")
        (self.hub / "home.md").write_text("# Home\n")
        commit_all(self.hub, "first hub entry")

    def tearDown(self):
        self._tmp.cleanup()

    def test_reads_commits_remotes_and_clean_tree(self):
        state = inspect_hub(self.hub, real_git)
        self.assertTrue(state.exists and state.is_repo)
        self.assertEqual(state.commits, 1)
        self.assertEqual(state.remotes, ())
        self.assertEqual(state.dirty, ())
        self.assertIsNone(state.enclosing_repo)

    def test_planted_dirty_file_reaches_the_warning(self):
        (self.hub / "finance.md").write_text("# Finance\n")
        state = inspect_hub(self.hub, real_git)
        self.assertIn("finance.md", state.dirty)
        self.assertIsNotNone(dirty_warning(self.hub, state))

    def test_planted_remote_silences_the_stale_warning(self):
        git("-C", str(self.hub), "remote", "add", "origin", "ssh://host.invalid/hubs.git")
        self.assertEqual(inspect_hub(self.hub, real_git).remotes, ("origin",))

    def test_planted_outer_repo_reaches_the_nested_warning(self):
        # PLANT: a dotfiles repo at the level above ~/hubs
        outer = make_repo(self.root)
        state = inspect_hub(self.hub, real_git)
        self.assertEqual(Path(state.enclosing_repo).resolve(), outer.resolve())
        self.assertIsNotNone(nested_repo_warning(self.hub, state))

    def test_an_old_store_with_no_remote_warns_but_does_not_block_start(self):
        # the end-to-end durability path: real repo -> real state -> warning -> exit 0
        marker = self.hub / ".buzai" / "remote-expected"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"# created\n{(NOW - timedelta(days=40)).isoformat()}\n")
        state = inspect_hub(self.hub, real_git)
        warns = warnings_for(self.hub, state, NOW)
        self.assertTrue(any("NO remote" in w for w in warns))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = report(HubTarget(self.hub, str(self.hub)), Findings(warnings=tuple(warns)))
        self.assertEqual(code, 0)

    def test_missing_hub_dir_is_a_note_not_a_warning(self):
        absent = self.root / "nope"
        state = inspect_hub(absent, real_git)
        self.assertFalse(state.exists)
        self.assertEqual(warnings_for(absent, state, NOW), [])
        self.assertIn("does not exist yet", notes_for(absent, state)[0])

    def test_directory_that_is_not_a_repo_is_a_note_not_a_warning(self):
        plain = self.root / "plain"
        plain.mkdir()
        state = inspect_hub(plain, real_git)
        self.assertTrue(state.exists)
        self.assertFalse(state.is_repo)
        self.assertEqual(warnings_for(plain, state, NOW), [])
        self.assertIn("not a git repository", notes_for(plain, state)[0])

    def test_marker_roundtrip_and_garbled_marker(self):
        marker = self.hub / ".buzai" / "remote-expected"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"# comment\n{NOW.isoformat()}\n")
        self.assertEqual(read_marker(self.hub), (NOW, ""))
        marker.write_text("# only a comment\n")
        created, error = read_marker(self.hub)
        self.assertIsNone(created)
        self.assertIn("unreadable", error)

    def test_no_marker_is_neither_a_timestamp_nor_an_error(self):
        self.assertEqual(read_marker(self.hub), (None, ""))

    def test_enclosing_repo_is_none_when_hub_is_its_own_repo_only(self):
        # asked from the parent, so the hub's own repo can never be the answer
        self.assertIsNone(enclosing_repo(self.hub, real_git))


class TestCredentialUsability(unittest.TestCase):
    """The perms check asks whether credentials.json is SAFE; this asks whether it WORKS.

    Only the first was ever asked, which is how a live instance sat dead for nine days:
    the OAuth tokens were emptied in place, the file stayed present and 0600, the
    preflight stayed green, and every start failed at `claude remote-control` with "You
    must be logged in". Nothing in the journal said why.
    """

    CREDS = Path("/home/example/.claude/.credentials.json")

    def warn(self, text):
        return credential_warning(text, self.CREDS)

    def oauth(self, **tokens):
        return json.dumps({"claudeAiOauth": tokens})

    def test_the_emptied_token_shape_that_caused_the_outage_is_named(self):
        # exactly what was on disk: present, 0600, parseable, and useless
        w = self.warn(self.oauth(accessToken="", refreshToken=""))
        self.assertIsNotNone(w)
        self.assertIn("make auth", w)

    def test_a_usable_access_token_is_silent(self):
        self.assertIsNone(self.warn(self.oauth(accessToken="tok", refreshToken="")))

    def test_a_usable_refresh_token_alone_is_silent(self):
        # an expired access token still refreshes; that is not an outage
        self.assertIsNone(self.warn(self.oauth(accessToken="", refreshToken="tok")))

    def test_whitespace_is_not_a_token(self):
        self.assertIsNotNone(self.warn(self.oauth(accessToken="   ", refreshToken="")))

    def test_a_missing_file_says_so(self):
        self.assertIn("make auth", self.warn(None))

    def test_unparseable_json_says_so(self):
        self.assertIn("JSON", self.warn("{not json"))

    def test_an_unrecognized_auth_shape_stays_silent(self):
        # saying nothing beats guessing: a wrong warning here teaches the operator to
        # ignore the one that matters
        self.assertIsNone(self.warn(json.dumps({"someOtherAuth": {"key": "v"}})))
        self.assertIsNone(self.warn(json.dumps({})))

    def test_no_warning_ever_carries_a_token_value(self):
        secret = "sk-ant-oat01-DO-NOT-ECHO"
        for text in (self.oauth(accessToken=secret, refreshToken=""), "{" + secret):
            w = self.warn(text) or ""
            self.assertNotIn(secret, w)

    def test_reader_maps_absent_and_unreadable_to_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(read_credentials(Path(d) / "nope.json"))


# --- the WIRING: assess() / main(), with the real composition ----------------------
#
# Every individual check above is well tested. `assess()` is what actually decides
# whether the service starts, and until now nothing exercised it: a check composed with
# the wrong constant, or dropped from the list entirely, would leave this whole file
# green while the deployed preflight stopped catching leaks. That is the dead-no-op
# failure class the module docstring warns about, one level up.
#
# So each test below plants ONE real violation under a tempdir, drives it through
# `main()` -> `assess()` -> `report()`, and asserts the composed verdict. Delete any
# line from `assess()`'s `fatal_problems(...)` call and the matching test fails.


class AssessCompositionCase(unittest.TestCase):
    """A whole fake instance in a tempdir: never the real home, checkout, or secrets.

    The fixture is a base class rather than the test class so a plant can be added in a
    subclass without re-running every test that already lives on it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.now = datetime.now(UTC)

        self.home = self.root / "home"
        self.home.mkdir()
        self.checkout = make_repo(self.root / "checkout")
        git("-C", str(self.checkout), "remote", "add", "origin", "https://example.invalid/b.git")
        self.hubs = self.checkout / "hubs"
        self.hubs.mkdir()
        (self.hubs / "README.md").write_text("# scaffold\n")
        (self.hubs / "_example-hub.md").write_text("# example\n")
        self.env_dir = self.checkout / "deploy" / "env"
        self.env_dir.mkdir(parents=True)
        (self.env_dir / "email.env.example").write_text("IMAP_PASSWORD=CHANGEME\n")
        commit_all(self.checkout, "scaffolds")

        self.secrets = self.home / ".config" / "buzai" / "secrets"
        self.secrets.mkdir(parents=True)
        self.creds = self.home / ".claude" / ".credentials.json"
        self.creds.parent.mkdir(parents=True)
        self.creds.write_text("{}")
        os.chmod(self.creds, 0o600)
        self.hub_key = self.home / ".ssh" / "buzai-hub"
        self.hub_key.parent.mkdir(parents=True)
        self.environ: dict[str, str] = {}

    def tearDown(self):
        self._tmp.cleanup()

    def seams(self, **overrides):
        seams = {
            "environ": self.environ,
            "home": self.home,
            "repo_root": self.checkout,
            "secrets_dir": self.secrets,
            "creds": self.creds,
            "env_dir": self.env_dir,
            "scaffold_dir": self.hubs,
            "hub_key": self.hub_key,
        }
        seams.update(overrides)
        return seams

    def findings(self, **overrides) -> Findings:
        return assess(self.now, **self.seams(**overrides))[1]

    def run_main(self, **overrides):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main([], **self.seams(**overrides))
        return code, out.getvalue(), err.getvalue()

    def assert_only_fatal(self, needle: str) -> None:
        """One planted violation -> exactly one fatal finding, and start is blocked."""
        findings = self.findings()
        self.assertEqual(len(findings.fatal), 1, findings.fatal)
        self.assertIn(needle, findings.fatal[0])
        self.assertTrue(findings.blocks_start)
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"secrets-preflight FAIL: {findings.fatal[0]}", err)


class TestAssessComposition(AssessCompositionCase):
    # --- the clean and warning-only baselines --------------------------------------

    def test_a_clean_instance_passes(self):
        findings = self.findings()
        self.assertEqual(findings.fatal, ())
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("secrets-preflight OK", out)

    def test_emptied_credentials_warn_through_assess_and_never_block_start(self):
        """PLANT: the nine-day outage's exact on-disk shape.

        A logged-out instance already fails at ExecStart; making this fatal would only
        add a second way to die, and with StartLimitBurst=5 the fatal path is the one
        that stops systemd retrying at all. The value is that the journal names the cause
        instead of leaving an operator to infer it from a restart loop.
        """
        self.creds.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
        )
        os.chmod(self.creds, 0o600)

        findings = self.findings()

        self.assertTrue([w for w in findings.warnings if "no usable token" in w], findings.warnings)
        self.assertEqual(findings.fatal, ())
        self.assertFalse(findings.blocks_start)
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)

    def test_a_durability_warning_composed_through_assess_still_exits_zero(self):
        # PLANT: a real hub repo with commits, no remote, and a marker old enough to
        # clear the grace period. Warning, never fatal — StartLimitBurst=5 would turn
        # "knowledge is not backed up" into "the assistant is gone".
        hub = make_repo(self.home / "hubs")
        (hub / "home.md").write_text("# Home\n")
        marker = hub / ".buzai" / "remote-expected"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"# created\n{(self.now - timedelta(days=40)).isoformat()}\n")
        commit_all(hub, "first hub entry")

        findings = self.findings()
        self.assertEqual(findings.fatal, ())
        self.assertTrue(any("NO remote" in w for w in findings.warnings))
        code, out, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("secrets-preflight WARN:", err)
        self.assertIn("start not blocked", out)

    # --- one planted leak per check `assess()` is supposed to compose ---------------

    def test_a_loose_secret_file_reaches_the_verdict(self):
        secret = self.secrets / "email.env"
        secret.write_text("IMAP_PASSWORD=hunter2\n")
        os.chmod(secret, 0o644)
        self.assert_only_fatal("must be 0600")

    def test_loose_credentials_json_reaches_the_verdict(self):
        os.chmod(self.creds, 0o644)
        self.assert_only_fatal(f"{self.creds} is 0o644")

    def test_a_real_env_file_in_the_checkout_reaches_the_verdict(self):
        real = self.env_dir / "email.env"
        real.write_text("IMAP_PASSWORD=hunter2\n")
        os.chmod(real, 0o600)  # perms are clean: existence alone is the violation
        self.assert_only_fatal("*.env.example scaffolds")

    def test_untracked_hub_content_in_the_checkout_reaches_the_verdict(self):
        (self.hubs / "finance-and-tax.md").write_text("# Finance\n")
        self.assert_only_fatal("personal hub content inside the public checkout")

    def test_tracked_hub_content_in_the_checkout_reaches_the_verdict(self):
        # PLANT: the P0 hole, end to end. Already in the public repo's index, and the
        # composed preflight must refuse to start rather than exempt it.
        leak = self.hubs / "personal.md"
        leak.write_text("# Personal\n")
        git("-C", str(self.checkout), "add", "--", "hubs/personal.md")
        commit_all(self.checkout, "oops")
        self.assert_only_fatal(f"git rm --cached {leak}")

    def test_a_loose_hub_deploy_key_reaches_the_verdict(self):
        self.hub_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        os.chmod(self.hub_key, 0o644)
        self.assert_only_fatal(f"{self.hub_key} is 0o644")

    def test_a_tracked_hub_deploy_key_reaches_the_verdict(self):
        # the key lives outside the checkout, so only a repo at ~ can track it — the
        # exact case a `git -C REPO_ROOT` tracked-check could never see
        dotfiles = make_repo(self.home)
        self.hub_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        os.chmod(self.hub_key, 0o600)
        git("-C", str(dotfiles), "add", "--", ".ssh/buzai-hub")
        commit_all(dotfiles, "track the deploy key")
        self.assert_only_fatal("tracked by git")

    def test_a_hub_path_inside_the_checkout_reaches_the_verdict(self):
        self.environ["BUZAI_HUBS_DIR"] = str(self.checkout / "hubs")
        self.assert_only_fatal("inside the repo checkout")

    def test_a_credential_helper_on_the_public_origin_reaches_the_verdict(self):
        git("-C", str(self.checkout), "config", "credential.helper", "store")
        self.assert_only_fatal("credential helper")

    # --- the seams are seams, not a second implementation --------------------------

    def test_the_resolved_hub_target_is_reported_from_the_injected_environment(self):
        self.environ["BUZAI_HUBS_DIR"] = str(self.root / "elsewhere")
        target, _ = assess(self.now, **self.seams())
        self.assertEqual(target.path, self.root / "elsewhere")
        self.assertIn("BUZAI_HUBS_DIR", target.description)

    def test_every_planted_leak_at_once_is_reported_together(self):
        # nothing short-circuits: one fatal finding must not hide the others
        os.chmod(self.creds, 0o644)
        (self.hubs / "finance-and-tax.md").write_text("# Finance\n")
        git("-C", str(self.checkout), "config", "credential.helper", "store")
        findings = self.findings()
        self.assertEqual(len(findings.fatal), 3, findings.fatal)

    # --- the wedge: an interrupted migration must have a way out --------------------

    def test_the_remedy_the_preflight_names_actually_unblocks_the_service(self):
        """PLANT: hub repo already created, personal content still in the checkout.

        The preflight is right to block — the content IS a leak — but `make hub-init`
        used to report "exists" and stop, so nothing could clear it and
        `StartLimitBurst=5` left the unit failed. This drives both halves.
        """
        hub = make_repo(self.home / "hubs")
        (hub / "README.md").write_text("# Hub store\n")
        commit_all(hub, "seeded")
        leak = self.hubs / "finance-and-tax.md"
        leak.write_text("# Finance\n\n- account 1234\n")

        blocked, _, err = self.run_main()
        self.assertEqual(blocked, 1)
        self.assertIn("make hub-init", err)
        self.assertIn("interrupted migration", err)

        result = initialize(hub, self.hubs, self.now)

        self.assertEqual(result.status, "resumed")
        self.assertFalse(leak.exists())
        self.assertEqual((hub / "finance-and-tax.md").read_text(), "# Finance\n\n- account 1234\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)


class TestTimeoutsDegradeInTheDirectionTheCheckDemands(unittest.TestCase):
    """PLANT: a git that never returns, on the `ExecStartPre` path.

    Unbounded, this is a hung service start, and five of those leave the unit `failed`.
    Bounded, what matters is the direction each check degrades in: a leak check that
    cannot answer must NOT pass, and a durability check that cannot answer must warn
    rather than take the assistant down with it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.slow = self.root / "slow-git"
        self.slow.write_text("#!/bin/sh\nsleep 2\n")
        self.slow.chmod(0o755)
        self.key = self.root / "buzai-hub"
        self.key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        os.chmod(self.key, 0o600)

    def tracked(self, path):
        return _git_tracked(path, git_bin=str(self.slow), timeout=0.3)

    # --- leak checks fail closed ---------------------------------------------------

    def test_a_hung_tracking_check_answers_with_a_reason_not_with_false(self):
        started = time.monotonic()
        answer = self.tracked(self.key)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsInstance(answer, str)
        self.assertIn("timed out", answer)

    def test_a_hung_tracking_check_is_fatal_rather_than_clean(self):
        probs = tracked_problems([self.key], self.tracked)
        self.assertEqual(len(probs), 1)
        self.assertIn("unverified rather than clean", probs[0])

    def test_it_reaches_the_fatal_list_and_blocks_start(self):
        fatal = fatal_problems(
            secret_files=[],
            repo_secrets=[],
            creds=self.root / "absent.json",
            hub_credentials=[self.key],
            hub_content=[],
            hub_path_problem=None,
            origin_violations=[],
            is_tracked=self.tracked,
        )
        self.assertEqual(len(fatal), 1, fatal)
        self.assertTrue(Findings(tuple(fatal)).blocks_start)

    def test_a_missing_git_is_also_unverified_rather_than_clean(self):
        answer = _git_tracked(self.key, git_bin=str(self.root / "no-such-git"), timeout=0.3)
        self.assertIsInstance(answer, str)
        self.assertIn("cannot run", answer)

    def test_a_hung_index_read_leaves_the_checkout_content_check_unverified(self):
        hubs = self.root / "hubs"
        hubs.mkdir()
        (hubs / "finance.md").write_text("# Finance\n")
        probs = hub_checkout_problems(hubs, str(self.slow), 0.3)
        self.assertTrue(any("unverified rather than clean" in p for p in probs), probs)
        self.assertTrue(any(str(hubs / "finance.md") in p for p in probs), probs)

    # --- durability checks warn ----------------------------------------------------

    def test_a_hung_hub_read_warns_and_does_not_block_start(self):
        hub = make_repo(self.root / "hubs")
        (hub / "home.md").write_text("# Home\n")
        commit_all(hub, "first hub entry")

        state = inspect_hub(hub, real_git, git_bin=str(self.slow), timeout=0.3)

        self.assertIn("timed out", state.inspect_error)
        warns = warnings_for(hub, state, NOW)
        self.assertTrue(any("timed out" in w for w in warns), warns)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = report(HubTarget(hub, str(hub)), Findings(warnings=tuple(warns)))
        self.assertEqual(code, 0)
        self.assertIn("secrets-preflight WARN", err.getvalue())


class TestANaiveStoredTimestampCannotBlockStart(AssessCompositionCase):
    """PLANT: a real hub store whose two stored timestamps have no UTC offset.

    `.buzai/remote-expected` and `.git/buzai/push-backlog.json` are plain files inside the
    owner's own repo. Hand-edited, restored from an older format, or written by a future
    code path, either can end up naive — and both are subtracted from an aware `now`
    inside WARNING-path checks (`stale_remote_warning`, `backlog_warning`) that
    `ExecStartPre` runs on every service start. Naive minus aware raises `TypeError`,
    which nothing on that path catches: the preflight died, `ExecStartPre` failed, and
    with `StartLimitBurst=5` the unit was left permanently `failed` — a *durability*
    condition, which cannot leak anything, taking the whole assistant offline. That is
    the exact inversion of the fatal/warning split this module exists to enforce.

    Fixed at the boundary in both readers (`hub_init.parse_marker` raises `ValueError`,
    `hub_commit.read_backlog` drops the entry), which is why this asserts on WHERE the
    save happened: the backstop in `durability_report` must not be what caught it.

    Verified against the unfixed version: `main()` raised `TypeError: can't subtract
    offset-naive and offset-aware datetimes` out of `assess`, so `ExecStartPre` failed.
    """

    def plant_naive_store(self):
        hub = make_repo(self.home / "hubs")
        (hub / "home.md").write_text("# Home\n")
        marker = hub / ".buzai" / "remote-expected"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"# created\n{(self.now - timedelta(days=40)).replace(tzinfo=None)}\n")
        commit_all(hub, "first hub entry")
        backlog = hub / ".git" / "buzai" / "push-backlog.json"
        backlog.parent.mkdir(parents=True, exist_ok=True)
        backlog.write_text(
            '{"pending": [{"commit": "deadbee", "recorded_at": "2026-08-05T12:00:00"}], '
            '"last_error": "offline", "last_attempt": "2026-08-05T12:00:00"}'
        )
        return hub

    def test_the_planted_timestamps_really_are_naive(self):
        # without this the class would pass against a store that never carried the fault
        hub = self.plant_naive_store()
        self.assertNotIn("+00:00", (hub / ".buzai" / "remote-expected").read_text())
        self.assertIsNone(
            datetime.fromisoformat(
                json.loads((hub / ".git" / "buzai" / "push-backlog.json").read_text())["pending"][
                    0
                ]["recorded_at"]
            ).tzinfo
        )

    def test_service_start_is_not_blocked(self):
        self.plant_naive_store()
        code, out, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertIn("secrets-preflight OK", out)

    def test_the_boundary_caught_it_not_the_backstop(self):
        # the backstop is defence in depth; if it is doing this job, the readers are not
        self.plant_naive_store()
        _, _, err = self.run_main()
        self.assertNotIn("durability checks could not be completed", err)

    def test_the_garbled_marker_is_reported_as_a_warning(self):
        self.plant_naive_store()
        warnings = self.findings().warnings
        self.assertTrue(any("unreadable" in w for w in warnings), warnings)
        self.assertTrue(any("grace period cannot be judged" in w for w in warnings), warnings)

    def test_the_naive_backlog_entry_is_dropped_rather_than_reported(self):
        self.plant_naive_store()
        self.assertFalse(any("not reached the remote" in w for w in self.findings().warnings))

    def test_an_aware_backlog_entry_is_still_warned_about(self):
        # the guard: dropping must not become "the backlog warning never fires"
        hub = self.plant_naive_store()
        (hub / ".git" / "buzai" / "push-backlog.json").write_text(
            '{"pending": [{"commit": "deadbee", "recorded_at": "2026-08-05T12:00:00+00:00"}], '
            '"last_error": "offline", "last_attempt": null}'
        )
        self.assertTrue(
            any("not reached the remote" in w for w in self.findings().warnings),
            self.findings().warnings,
        )


class TestAnUnexpectedDurabilityFailureDegradesToAWarning(AssessCompositionCase):
    """PLANT: a durability reader that raises an exception nothing on that path catches.

    Defence in depth behind the two boundary fixes above. Every anticipated failure — a
    hung git, an unparseable marker — already degrades correctly and has its own test;
    this is the backstop for the *unanticipated* one, which is what a naive stored
    timestamp was until it was found. `ExecStartPre` failure blocks service start and
    `StartLimitBurst=5` makes five of them permanent, so no bug on the durability path
    may be able to reach that outcome: nothing there can leak, so nothing there is worth
    ending the assistant over.

    The runner raises only for calls targeting the hub store, so the FATAL path — which
    uses the same runner against the checkout — is untouched and can still be asserted on.

    Verified against the unfixed version (no try/except in `assess`): `main()` propagated
    `RuntimeError` and `ExecStartPre` failed.
    """

    def exploding_runner(self, hub):
        def runner(args, env, timeout):
            if str(hub) in " ".join(args):
                raise RuntimeError("the hub reader blew up")
            return real_git(args, env, timeout)

        return runner

    def planted(self):
        hub = make_repo(self.home / "hubs")
        (hub / "home.md").write_text("# Home\n")
        commit_all(hub, "first hub entry")
        return hub, self.exploding_runner(hub)

    def test_it_warns_and_exits_zero(self):
        _, runner = self.planted()
        code, out, err = self.run_main(runner=runner)
        self.assertEqual(code, 0, err)
        self.assertIn("durability checks could not be completed", err)
        self.assertIn("RuntimeError", err)
        self.assertIn("secrets-preflight OK", out)

    def test_it_is_a_warning_and_never_a_fatal(self):
        _, runner = self.planted()
        findings = self.findings(runner=runner)
        self.assertEqual(findings.fatal, ())
        self.assertFalse(findings.blocks_start)
        self.assertEqual(len(findings.warnings), 1, findings.warnings)

    def test_a_planted_leak_still_blocks_start_alongside_it(self):
        # the guard: the backstop must not have softened the FATAL half
        _, runner = self.planted()
        (self.hubs / "finance-and-tax.md").write_text("# Finance\n")
        code, _, err = self.run_main(runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("secrets-preflight FAIL", err)
        self.assertIn("finance-and-tax.md", err)
        self.assertIn("durability checks could not be completed", err)

    def test_a_leak_check_that_cannot_answer_is_still_fatal(self):
        # the other half of the guard: "cannot tell" on the leak side never reads as clean
        self.hub_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        os.chmod(self.hub_key, 0o600)
        findings = self.findings(is_tracked=lambda p: "git could not say")
        self.assertTrue(findings.blocks_start)
        self.assertTrue(any("unverified rather than clean" in f for f in findings.fatal))


if __name__ == "__main__":
    unittest.main()
