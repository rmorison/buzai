import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.hub_commit import Backlog
from scripts.hub_remote import default_git_runner as real_git
from scripts.secrets_preflight import (
    REMOTE_GRACE_SECONDS,
    Findings,
    HubState,
    HubTarget,
    _git_tracked,
    _repo_secret_candidates,
    backlog_warning,
    dirty_warning,
    enclosing_repo,
    env_file_problems,
    fatal_problems,
    hub_checkout_problems,
    hub_content_problems,
    inspect_hub,
    nested_repo_warning,
    notes_for,
    origin_credential_violations,
    perm_problems,
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
        # a hubs/ directory that is not inside any git repo: git cannot say what is a
        # scaffold, and "cannot tell" must never read as "clean"
        outside = Path(self._tmp.name) / "loose-hubs"
        outside.mkdir()
        (outside / "x.md").write_text("x")
        probs = hub_checkout_problems(outside)
        self.assertEqual(len(probs), 1)
        self.assertIn("unverified", probs[0])

    def test_message_names_the_remedy(self):
        probs = hub_content_problems(["finance.md"], Path("/repo/hubs"))
        self.assertIn("/repo/hubs/finance.md", probs[0])
        self.assertIn("make hub-init", probs[0])


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


if __name__ == "__main__":
    unittest.main()
